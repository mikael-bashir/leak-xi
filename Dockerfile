# Leak XI — Mathlib retrieval for the Architect stack.
#
# Same architecture as Leak-I (persistent `loogle -i --json` daemon + a
# Chroma/MiniLM semantic index, served over FastMCP/SSE), rebuilt against the
# toolchain the Leak X series compiles with. That match is the whole point:
# Leak XII and Leak XIV elaborate at v4.32.0, so retrieval must answer from
# v4.32.0 or it invents names the prover's Lean does not have.
#
# Two differences from Leak-I, both deliberate:
#
#  * Mathlib is pinned to the RELEASE TAG matching the toolchain rather than
#    tracking `master`. Leak-I overwrites lean-toolchain and lets `lake update`
#    resolve whatever master happens to be that day, which is only safe while
#    master is close to the pinned toolchain. Pinning the tag makes the build
#    reproducible and guarantees `lake exe cache get` has a cache to fetch.
#
#  * The moogle index is built HERE, from the packages lake just resolved,
#    instead of being committed to the repo as a 220 MB LFS blob. The semantic
#    half then cannot disagree with the exact half about which Mathlib exists,
#    and it picks up Lean core and Batteries for free.
#
# NOTE ON RISK: upstream loogle currently targets v4.30.0-rc1 (its own
# lean-toolchain), so forcing v4.32.0 asks its Lean sources to compile across
# two minor releases. That is the one step here that can fail. If it does, the
# fix is a one-line change to TOOLCHAIN/MATHLIB_TAG below, or bumping
# LOOGLE_REV to a revision that targets v4.32 once upstream publishes one.

FROM ubuntu:22.04

RUN useradd -m -u 1000 user

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential python3 python3-pip python3-venv ca-certificates && \
    rm -rf /var/lib/apt/lists/*

USER user
ENV HOME=/home/user
ENV PATH="${HOME}/.local/bin:${HOME}/.elan/bin:${PATH}"

RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# --- Lean side: loogle, built against the Leak X toolchain -------------------
# ba48a42e is upstream's most recent commit that still DEPENDS on Mathlib.
# loogle master (9f11169) has no `require mathlib` and an empty lake-manifest —
# it builds fine and indexes nothing, which is the worst possible outcome here
# (a search server that confidently returns zero hits for everything). Do not
# bump this to master without re-checking that the require line is back.
ARG LOOGLE_REV=ba48a42e11
ARG TOOLCHAIN=leanprover/lean4:v4.32.0
ARG MATHLIB_TAG=v4.32.0

WORKDIR ${HOME}
RUN git clone https://github.com/nomeata/loogle.git
WORKDIR ${HOME}/loogle
RUN git checkout ${LOOGLE_REV}

# Force the toolchain, and pin Mathlib to the tag that ships it, so lake
# resolves a self-consistent set instead of master-of-the-day.
#
# Both edits are ASSERTED, not attempted: a sed that silently misses would
# leave the build pointing at Mathlib master on a forced toolchain — it would
# probably still produce an image, and that image would answer from the wrong
# library. Fail the build here instead of debugging it in production.
RUN echo "${TOOLCHAIN}" > lean-toolchain && \
    sed -i "s|leanprover-community/mathlib4\" @ \"master\"|leanprover-community/mathlib4\" @ \"${MATHLIB_TAG}\"|" lakefile.lean && \
    grep -q "mathlib4\" @ \"${MATHLIB_TAG}\"" lakefile.lean \
      || { echo "FATAL: could not pin Mathlib to ${MATHLIB_TAG} — lakefile.lean shape changed upstream:"; cat lakefile.lean; exit 1; }
RUN grep -q "^${TOOLCHAIN}$" lean-toolchain \
      || { echo "FATAL: toolchain not pinned"; cat lean-toolchain; exit 1; }

RUN lake update
# Pre-compiled Mathlib oleans, so the image build does not have to compile
# Mathlib (which no CI budget survives).
RUN lake exe cache get
RUN lake build

# --- Python side: MCP server + moogle index ---------------------------------
#
# LAYER ORDER IS LOad-BEARING HERE. Everything below is ordered cheapest-to-
# invalidate last. Copying the whole repo up front — which is what this did —
# meant every edit to server.py invalidated the copy, and therefore the pip
# installs, the encoder fetch AND the 20-minute embedding pass beneath it.
# Three server.py edits in one afternoon cost three full index rebuilds for no
# reason: the index does not depend on server.py in any way.
#
# So: only the indexer and its inputs are copied before the index is built.
# server.py lands afterwards, where a change to it costs a few seconds.
WORKDIR ${HOME}/app

RUN uv python install 3.11
RUN uv venv --python 3.11 ${HOME}/app/.venv
ENV PATH="${HOME}/app/.venv/bin:${PATH}"

# torch FIRST, from PyTorch's CPU index.
#
# sentence-transformers depends on torch, and torch's default PyPI wheel for
# linux-x86_64 is the CUDA build: it drags in nvidia-cublas (403 MiB),
# nvidia-cudnn (349 MiB), nvidia-cusolver (192 MiB) and friends. Measured on
# the previous build of this Space: 2589 MiB of the 2747 MiB pip download —
# 94% — was GPU runtime, on a CPU-only Space that can never execute a single
# byte of it. It is re-fetched on every rebuild and it sits in the image, so
# every cold start pays to pull it too.
#
# Installing torch from the CPU index first means the next resolve sees the
# requirement already satisfied and never reaches for the CUDA wheel.
RUN uv pip install --index-url https://download.pytorch.org/whl/cpu torch
RUN uv pip install fastmcp nest_asyncio "uvicorn[standard]" chromadb sentence-transformers

# Pin the model cache to a path inside the image, for BOTH the build below and
# the runtime server. Left to the default (~/.cache/huggingface) it depends on
# HOME and on whatever HF Spaces injects into the runtime environment; if those
# ever disagree, the server silently re-downloads the encoder on every start
# instead of reading the copy already baked into the image.
ENV HF_HOME=/home/user/app/.hfcache
ENV SENTENCE_TRANSFORMERS_HOME=/home/user/app/.hfcache

# Fetch the encoder as its own layer, so editing the indexer below does not
# re-download it.
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# The moogle index is NOT built here. It ships in the repo, LFS-backed, exactly
# as Leak-I has shipped its own for months — `chroma_db/** filter=lfs` in
# .gitattributes, and this COPY brings it in.
#
# Building it in the image was the mistake. The index is a pure function of
# (Mathlib rev, encoder, build_moogle.py); none of those change when server.py
# does, yet every Dockerfile or source edit recomputed all 72,876 embeddings —
# 20 minutes, several times over, for nothing. Layer ordering only narrowed the
# set of edits that triggered it; editing this file still did.
#
# Regenerate with build_moogle.py (which stays in the repo) when Mathlib or the
# encoder moves, and commit the result. That is the only time the cost is real.
COPY --chown=user . ${HOME}/app

# From here on the hub is off limits. The encoder is already in the image, so a
# runtime hub call could only mean the cache lookup missed — and a fast, loud
# failure (moogle reports itself unavailable, loogle keeps serving) is better
# than a silent multi-hundred-MB download on a cold start. Set AFTER the two
# build steps above so they still run online.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

ENV LOOGLE_DIR=${HOME}/loogle
ENV CHROMA_DIR=${HOME}/app/chroma_db
ENV LEAN_TOOLCHAIN=v4.32.0

EXPOSE 7860
CMD ["python3", "server.py"]

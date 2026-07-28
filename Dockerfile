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
WORKDIR ${HOME}/app
COPY --chown=user . ${HOME}/app

RUN uv python install 3.11
RUN uv venv --python 3.11 ${HOME}/app/.venv
ENV PATH="${HOME}/app/.venv/bin:${PATH}"
RUN uv pip install fastmcp nest_asyncio "uvicorn[standard]" chromadb sentence-transformers

# Build moogle from the SAME sources loogle was compiled against (Mathlib +
# Batteries + Lean core). A failure here must not cost us the exact-search
# half, so it is non-fatal: server.py loads the index lazily and degrades to
# loogle-only with an explicit message.
RUN python3 build_moogle.py ${HOME}/loogle/.lake/packages ${HOME}/app/chroma_db \
    || echo "⚠️ moogle index build failed — Leak XI will serve loogle only"

ENV LOOGLE_DIR=${HOME}/loogle
ENV CHROMA_DIR=${HOME}/app/chroma_db
ENV LEAN_TOOLCHAIN=v4.32.0

EXPOSE 7860
CMD ["python3", "server.py"]

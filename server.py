"""Leak XI — Mathlib retrieval for the Architect stack, on the Leak X toolchain.

Replaces the previous bespoke SQLite/FTS5 index with Leak-I's architecture —
a persistent `loogle -i --json` daemon plus a Chroma/MiniLM semantic index —
rebuilt against **v4.32.0**, the toolchain Leak XII and Leak XIV compile with.
Matching the toolchain is the entire point: a retrieval tool that answers from
a different Mathlib than the prover's compiler is a name generator, not a
lookup, and every name it invents costs a compile turn.

WHY THE REWRITE. Two defects in the FTS index, both measured against a real
`floors_recover_whole` run:

1. NO COVERAGE OF LEAN CORE. The indexer walked `<mathlib>/Mathlib` only, so
   the core `Nat`/`Int`/`List` API did not exist as far as search was
   concerned. `Nat.mul_div_assoc` closed a node in that very run
   (`exact Nat.mul_div_assoc c h`), and querying it returned five unrelated
   declarations and no sign that the real one was simply not indexed. loogle
   has no such hole: it indexes the environment, so whatever `import Mathlib`
   brings in is searchable.

2. IT COULD NOT SAY "NO". Every query fell back from AND to OR matching and
   returned its best partial matches, so `Nat.div_eq_zero_of_dvd` — which does
   not exist — came back with `Nat.maxPowDvdDiv_zero_left` and friends. The
   prover read that as a near-miss and spent the run trying variants of a name
   that was never there. A search that cannot return a falsifiable negative
   teaches nothing.

The split into two tools is deliberate: they answer different questions, and
merging them destroys the negative answer. `loogle_search` is exact and
falsifiable (a pattern that matches nothing is a PROOF of absence);
`moogle_search` is fuzzy and always returns its best guesses, which is right
for discovery and useless as evidence. `mathlib_search` is kept as a
compatibility entry point for clients that predate the split.
"""

import asyncio
import json
import logging
import os
import re

import nest_asyncio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

nest_asyncio.apply()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("leak-xi")

HOME = os.environ.get("HOME", "/home/user")
LOOGLE_DIR = os.environ.get("LOOGLE_DIR", os.path.join(HOME, "loogle"))
CHROMA_DIR = os.environ.get("CHROMA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_db"))
TOOLCHAIN = os.environ.get("LEAN_TOOLCHAIN", "v4.32.0")

# Per-query cap (a warm index answers in <5s; broad queries can be slow). This
# is a backstop, not the normal path — and a timeout does NOT kill the daemon,
# so one slow query cannot poison the next.
QUERY_TIMEOUT = 45.0
# The first query has to load all of Mathlib's index into RAM — minutes on a
# small CPU. The warm query blocks up to this long.
INDEX_LOAD_TIMEOUT = 900.0

mcp = FastMCP(
    "Leak-XI",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ==========================================
# LOOGLE DAEMON (persistent background process)
# ==========================================
# Ported from Leak-I with its hardening intact, because every line of it was
# paid for by a real outage:
#   * loogle interleaves banners and heartbeat JSON on stdout, so a single
#     readline + json.loads grabs the wrong line. We read until a real result
#     object ({"hits": …} / {"error": …}) arrives, tolerating multi-line JSON.
#   * killing the process on a timeout drops the warm Mathlib index and forces
#     a ~3-5 min cold reload that poisons every later query. A slow query is
#     SLOW, not dead: leave it warm and discard the late result instead.
#   * an empty line makes loogle treat stdin as closed and exit; a bare integer
#     makes it elaborate a term and burn its whole heartbeat budget. Both are
#     rejected before they reach the daemon.
class LoogleDaemon:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.is_ready = False
        self.last_error = ""
        # loogle emits exactly one result object per query line. When a query is
        # abandoned (times out) we DON'T kill the still-warm daemon — instead we
        # count the abandoned query here so its late-arriving result is discarded
        # (not mis-returned) before the next query's real result is read.
        self._pending_abandoned = 0

    async def boot(self):
        """Spawn loogle. Does NOT wait for the index — _ensure_ready() does."""
        if self.process and self.process.returncode is None:
            return
        logger.info("🚨 [LOOGLE] starting `loogle -i --json` (index load ~3-5 min)…")
        self.is_ready = False
        self.process = await asyncio.create_subprocess_exec(
            "./.lake/build/bin/loogle", "-i", "--json",
            cwd=LOOGLE_DIR,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._log_stderr(self.process))

    async def _log_stderr(self, proc):
        if not proc or not proc.stderr:
            return
        while True:
            try:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.info(f"[LOOGLE-STDERR] {line.decode('utf-8', 'replace').rstrip()}")
            except Exception:
                break

    async def _drain(self):
        """Consume any pending/stale stdout (leftover result, heartbeats) so the
        next read is for the query we are about to send."""
        if not self.process or not self.process.stdout:
            return
        while True:
            try:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=0.15)
                if not line:
                    break
            except asyncio.TimeoutError:
                break

    async def _read_result(self, timeout: float) -> dict:
        """Read stdout until loogle emits a real result object (has 'hits' or
        'error'), skipping banners/prompts/heartbeats and tolerating multi-line
        JSON. Raises asyncio.TimeoutError or EOFError."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        pending = ""
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=remaining)
            if not line:
                raise EOFError("loogle closed stdout")
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            candidate = (pending + s) if pending else s
            try:
                obj = json.loads(candidate)
                pending = ""
            except json.JSONDecodeError:
                # Either a multi-line JSON still arriving, or a non-JSON banner.
                if candidate.lstrip()[:1] in ("{", "["):
                    pending = candidate  # accumulate the rest of the object
                else:
                    logger.info(f"[LOOGLE noise] {candidate[:120]}")
                    pending = ""
                continue
            if isinstance(obj, dict) and ("hits" in obj or "error" in obj):
                # A real result object. If earlier queries were abandoned on
                # timeout, their results surface here first — discard exactly that
                # many so THIS query gets its own answer, not a stale one.
                if self._pending_abandoned > 0:
                    self._pending_abandoned -= 1
                    logger.info(
                        f"[LOOGLE] discarded late result from an abandoned query "
                        f"({self._pending_abandoned} still pending)"
                    )
                    continue
                return obj
            logger.info(f"[LOOGLE skip] {str(obj)[:80]}")

    async def _ensure_ready(self):
        """Caller MUST hold self.lock. Boot if needed and block until the index
        is loaded (confirmed by a real result to a trivial warm query)."""
        if self.is_ready and self.process and self.process.returncode is None:
            return
        await self.boot()
        logger.info("⏳ [LOOGLE] loading Mathlib index (blocking a warm query)…")
        await self._drain()
        self.process.stdin.write(b"Nat.add_comm\n")
        await self.process.stdin.drain()
        await self._read_result(INDEX_LOAD_TIMEOUT)
        self.is_ready = True
        self.last_error = ""
        logger.info("✅ [LOOGLE] index resident — searches are fast now.")

    async def warmup(self):
        """Background pre-load so uvicorn can open the port immediately."""
        async with self.lock:
            try:
                await self._ensure_ready()
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"⚠️ [LOOGLE] warmup failed: {e}")

    def _reset(self):
        """Kill the (wedged/dead) process so the next call reboots cleanly. Only
        for genuinely-dead states (EOF, failed boot, wedged past the safety
        valve) — NEVER for an ordinary slow query, which just drops the warm
        index and forces a multi-minute reload that poisons every later query."""
        self.is_ready = False
        self._pending_abandoned = 0
        try:
            if self.process:
                self.process.kill()
        except Exception:
            pass

    @staticmethod
    def _reject_reason(query: str) -> str | None:
        """Reject queries that are pathological for loogle BEFORE they reach it —
        one bad query used to stall the daemon for everyone. A bare integer
        (e.g. "1680") makes loogle elaborate it as a term and burn its whole
        heartbeat budget."""
        q = query.strip()
        if re.fullmatch(r"[+-]?\d+", q):
            return (
                "Bare numbers aren't searchable — loogle needs a TYPE PATTERN, a "
                'name substring in quotes, or a constant. Try e.g. `Nat.factorial`, '
                '`"add_comm"`, or a pattern like `_ ^ 2 + _`.'
            )
        return None

    async def search(self, query: str, timeout: float = QUERY_TIMEOUT) -> dict:
        if not query or not query.strip():
            return {"error": "Empty query. Give a Lean pattern (e.g. `_ ^ 2`), a name substring in quotes (e.g. \"add_comm\"), or a constant (e.g. `Real.sin`)."}
        reject = self._reject_reason(query)
        if reject:
            logger.info(f"[LOOGLE] rejected pathological query {query!r} without hitting loogle")
            return {"error": reject}
        async with self.lock:
            try:
                await self._ensure_ready()
            except Exception as e:
                logger.error(f"[LOOGLE] could not start: {e}")
                self.last_error = str(e)
                self._reset()
                return {"error": f"loogle backend failed to start: {e}"}

            # Only drain stale output when nothing is outstanding. If earlier
            # queries were abandoned, their results are accounted for by
            # _pending_abandoned and skipped inside _read_result — draining here
            # would silently eat them and desync the skip count.
            if self._pending_abandoned == 0:
                await self._drain()
            try:
                self.process.stdin.write((query + "\n").encode("utf-8"))
                await self.process.stdin.drain()
                return await self._read_result(timeout)
            except asyncio.TimeoutError:
                self._pending_abandoned += 1
                logger.error(
                    f"[LOOGLE] query slow (> {timeout:.0f}s): {query!r} — abandoned, "
                    f"index kept warm ({self._pending_abandoned} pending)"
                )
                if self._pending_abandoned >= 3:
                    logger.error("[LOOGLE] too many stuck queries — daemon looks wedged, rebooting once")
                    self._reset()
                return {"error": "Query timed out (too broad/complex). Anchor it with a specific constant (e.g. `Nat`, `Real.sin`) or make it narrower."}
            except EOFError:
                logger.error("[LOOGLE] daemon EOF — will reboot next call")
                self._reset()
                return {"error": "loogle backend restarted — retry the query."}
            except Exception as e:
                logger.error(f"[LOOGLE] I/O error: {e}")
                self.last_error = str(e)
                self._reset()
                return {"error": f"loogle I/O error: {e}"}


loogle_engine = LoogleDaemon()


# ==========================================
# MOOGLE (semantic index) — lazily loaded, never fatal
# ==========================================
# Leak-I loads the embedding model and opens the collection at import time, so
# a missing or corrupt index takes the whole server down and loogle with it.
# Here moogle is loaded on first use and every failure is contained: if the
# semantic half is unavailable, the exact half still answers, which is the half
# the prover cannot work without.
class Moogle:
    def __init__(self):
        self._ready = False
        self._error = ""
        self._model = None
        self._coll = None
        self._lock = asyncio.Lock()

    def _load(self):
        import chromadb
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(os.environ.get("MOOGLE_MODEL", "all-MiniLM-L6-v2"))
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        self._coll = client.get_collection(name="moogle")
        self._ready = True
        logger.info(f"✅ [MOOGLE] semantic index online ({self._coll.count()} entries).")

    async def ensure(self) -> bool:
        if self._ready:
            return True
        async with self._lock:
            if self._ready:
                return True
            try:
                await asyncio.to_thread(self._load)
            except Exception as e:
                self._error = str(e)
                logger.error(f"⚠️ [MOOGLE] unavailable: {e}")
                return False
        return True

    async def query(self, concept: str, k: int = 10) -> list[dict]:
        if not await self.ensure():
            raise RuntimeError(self._error or "moogle index unavailable")

        def run():
            vec = self._model.encode([concept]).tolist()
            return self._coll.query(query_embeddings=vec, n_results=max(1, min(k, 30)),
                                    include=["documents", "metadatas"])

        res = await asyncio.to_thread(run)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        return [{"doc": docs[i], **(metas[i] or {})} for i in range(len(docs))]


moogle_engine = Moogle()


# ==========================================
# Rendering
# ==========================================
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'!?]*(?:\.[A-Za-z0-9_'!?]+)*$")


def is_name_query(q: str) -> bool:
    """A bare identifier — the shape whose negative answer is worth stating."""
    return bool(NAME_RE.fullmatch(q.strip()))


def render_loogle(result: dict, query: str) -> str:
    if not isinstance(result, dict):
        return "Loogle Error: unexpected backend response."
    if result.get("error"):
        return f"Loogle Error: {result['error']}"

    hits = result.get("hits", [])
    if not hits:
        # THE POINT OF THIS TOOL. loogle searches the elaborated environment, so
        # zero hits for a name is not "try harder" — it is evidence the name is
        # not in this Mathlib. Say so in terms the caller cannot mistake for a
        # near-miss, because reading a near-miss into a miss is exactly what
        # sends a prover through five variants of a name that never existed.
        if is_name_query(query):
            return (
                f"NO DECLARATION NAMED `{query}` EXISTS in Mathlib @ {TOOLCHAIN}.\n"
                f"This is a definitive negative: loogle searches the elaborated environment "
                f"(Mathlib + Batteries + Lean core), not a text index.\n"
                f"Do NOT retry a near-variant of this name — a name that fails is usually a "
                f"whole naming CONVENTION that fails. Search for the FACT instead: give "
                f"loogle_search a type pattern (e.g. `?n / ?d = 0`) or call moogle_search "
                f"with the concept in English."
            )
        return (
            f"NO RESULTS for `{query}` in Mathlib @ {TOOLCHAIN}. Nothing in the environment "
            f"matches that pattern — the pattern is either too specific or shaped wrong. "
            f"Loosen one component (`_` for a subterm), or call moogle_search with the "
            f"concept in English."
        )

    count = result.get("count", len(hits))
    shown = hits[:12]
    out = [f"{count} result(s) in Mathlib @ {TOOLCHAIN}" + (f" (showing {len(shown)})" if count > len(shown) else "") + ":\n"]
    for hit in shown:
        line = f"{hit.get('name')} : {hit.get('type')}"
        mod = hit.get("module")
        out.append(line + (f"\n    [{mod}]" if mod else ""))
    return "\n".join(out)


def render_moogle(rows: list[dict], concept: str) -> str:
    if not rows:
        return f"No semantic matches for '{concept}'."
    out = [
        f"{len(rows)} semantically related declaration(s) for '{concept}'.",
        "These are SUGGESTIONS ranked by meaning, not matches — the names are real "
        "(indexed from Mathlib @ " + TOOLCHAIN + ") but none is guaranteed to state what you asked for.\n",
    ]
    for r in rows:
        out.append(f"{r.get('kind', 'decl')} {r.get('name')} : {r.get('signature', '')}".rstrip())
        if r.get("doc"):
            out.append(f"    -- {r['doc'][:300]}")
    return "\n".join(out)


# ==========================================
# TOOL 1: LOOGLE — exact, falsifiable
# ==========================================
@mcp.tool()
async def loogle_search(query: str) -> str:
    """
    EXACT search of the Lean 4 Mathlib environment by name or type pattern.
    Use this when you know WHAT you want and need the real name or signature.

    This tool can say NO. It searches the elaborated environment, so zero
    results for a name is definitive evidence that the name does not exist —
    treat it as a fact, not as a hint to try a variant. That negative is the
    reason to prefer this tool over moogle_search whenever the query can be
    expressed as a name or a pattern.

    SYNTAX (Lean, not English — use moogle_search for English):
    1. By constant:        Real.sqrt ?a * Real.sqrt ?a
    2. By name substring:  "add_comm"      (plain quotes, no backslashes)
    3. By conclusion:      |- tsum _ = _ * tsum _
    4. Comma = AND:        Real.sin, "pi"
    5. `_` is a wildcard subterm, `?a` a named metavariable; patterns are
       order-invariant in their arguments.

    ALWAYS anchor with a concrete constant (`Nat`, `Real.sin`, `0`) or a
    metavariable. Unanchored queries like (_ + _ = _ + _) scan the whole
    library and time out.

    Good queries:
    - Nat.mul_div_assoc
    - "div_eq_zero"
    - ?n / ?d = 0
    - _ ^ 2 - _ ^ 2, |- _ = _ * _, "sq"
    """
    logger.info(f"loogle_search: {query!r}")
    return render_loogle(await loogle_engine.search(query), query)


# ==========================================
# TOOL 2: MOOGLE — semantic discovery
# ==========================================
@mcp.tool()
async def moogle_search(concept: str, k: int = 10) -> str:
    """
    SEMANTIC search of Mathlib by natural-language concept. Use this when you
    know the mathematics in English but not the Lean name.

    This tool CANNOT say no: it always returns its nearest neighbours, so an
    unhelpful result set is not evidence about what exists. Never conclude a
    name is absent from a moogle_search miss — ask loogle_search.

    GUIDELINES:
    1. Plain English ('mean value theorem', 'multiplying by zero'). No Lean
       wildcards (_), metavariables (?a), or code.
    2. Use it to DISCOVER naming conventions (that 'square' is `sq` or
       `mul_self`, that 'divides' is `dvd`).
    3. Once you have a candidate name or convention, PIVOT to loogle_search to
       confirm it exists and get the exact signature.

    Examples:
    - "difference of squares"
    - "a number divided by something that divides it"
    - "triangle inequality for complex numbers"
    """
    logger.info(f"moogle_search: {concept!r}")
    if not concept or not concept.strip():
        return "Empty concept. Describe the mathematics in English, e.g. 'difference of squares'."
    try:
        return render_moogle(await moogle_engine.query(concept, k), concept)
    except Exception as e:
        return (
            f"moogle (semantic search) is unavailable: {e}\n"
            f"Use loogle_search instead — a name substring in quotes (e.g. \"div_eq\") is the "
            f"closest equivalent for discovery."
        )


# ==========================================
# TOOL 3: mathlib_search — compatibility entry point
# ==========================================
@mcp.tool()
async def mathlib_search(query: str, k: int = 12) -> str:
    """
    Compatibility entry point that routes to loogle_search or moogle_search by
    the shape of the query. Prefer calling those two directly — they document
    what their answers mean, and this router has to guess.

    Lean-shaped queries (a name, a quoted substring, a type pattern) go to
    loogle. English prose goes to moogle. A loogle miss on a name-shaped query
    is reported as the definitive negative it is, followed by semantic
    suggestions that are clearly labelled as DIFFERENT declarations.
    """
    q = (query or "").strip()
    if not q:
        return "Empty query. Give a Lean name/pattern, or describe the concept in English."

    lean_shaped = is_name_query(q) or bool(re.search(r'[_?()|\[\]{}^*+/\\=<>∀∃∣≤≥→↔·]|"', q))
    logger.info(f"mathlib_search: {q!r} -> {'loogle' if lean_shaped else 'moogle'}")

    if not lean_shaped:
        try:
            rows = await moogle_engine.query(q, k)
            if rows:
                return render_moogle(rows, q)
        except Exception as e:
            logger.error(f"[compat] moogle leg failed: {e}")
        return render_loogle(await loogle_engine.search(q), q)

    result = await loogle_engine.search(q)
    rendered = render_loogle(result, q)
    if result.get("hits"):
        return rendered
    # A miss. Keep the definitive negative FIRST and in full, then offer
    # semantic candidates under a heading that cannot be misread as "did you
    # mean" — they are other declarations, not spellings of the one asked for.
    try:
        rows = await moogle_engine.query(q.replace("_", " ").replace(".", " "), min(k, 8))
        if rows:
            return rendered + "\n\n--- Semantically related declarations (DIFFERENT names, none is the one above) ---\n" + render_moogle(rows, q)
    except Exception:
        pass
    return rendered


async def main_serve():
    logger.info(f"Booting Leak XI (loogle + moogle) @ Mathlib {TOOLCHAIN}…")

    # Warm the loogle Mathlib index in the BACKGROUND so the port opens right
    # away (HF marks the Space healthy; moogle is usable immediately). The daemon
    # lock makes the first loogle_search wait behind the warmup instead of racing
    # it — which is what previously kept the index from ever loading.
    asyncio.create_task(loogle_engine.warmup())

    http_app = mcp.sse_app()
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*", "mcp-protocol-version", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )

    logger.info("🌐 Serving Leak XI MCP (SSE) on 0.0.0.0:7860")
    config = uvicorn.Config(
        http_app,
        host="0.0.0.0",
        port=7860,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main_serve())

"""Leak XI — `mathlib_search`, the retrieval tool of the Architect stack.

Real FastMCP server (matching Leak-I/II's own wrapper architecture exactly:
mcp.server.fastmcp.FastMCP, @mcp.tool()-decorated functions, served via
mcp.sse_app() + uvicorn on 7860) rather than a bespoke REST API — so it is a
genuine MCP server the app's existing "Add Server" flow can register and
live-handshake against, like every other Leak server.

Contract (paper C.2): a lookup helper for *specific* Mathlib lemmas — a
name, a signature fragment, a hypothesis pattern ("monotonicity of natural
number addition", "Cauchy-Schwarz inequality"), or recovering the right
name after an "Unknown constant" error. Returns compact, citeable results.

Query handling: FTS5 lexical match over names (with camelCase/snake_case
subtoken expansion + a math-English synonym table), signatures, and
docstrings; graceful fallback from strict AND to OR matching so near-miss
queries still return candidates.
"""

import asyncio
import logging
import os
import re
import sqlite3

import nest_asyncio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

nest_asyncio.apply()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("leak-xi")

DB_PATH = os.environ.get("DB_PATH", "/opt/index/mathlib.db")

mcp = FastMCP(
    "Leak-XI",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

con = sqlite3.connect(DB_PATH, check_same_thread=False)
con.row_factory = sqlite3.Row

MATH_SYNONYMS = {
    # English math vocabulary -> Mathlib naming tokens. The paper's canonical
    # queries ("monotonicity of natural number addition") arrive in English;
    # Mathlib names speak in abbreviations (Nat.add_le_add_left). Expanding
    # the query with both sides closes that gap without any embedding model.
    "multiplication": ["mul"], "multiply": ["mul"], "times": ["mul"], "product": ["mul", "prod"],
    "addition": ["add"], "plus": ["add"], "sum": ["add", "sum"],
    "subtraction": ["sub"], "minus": ["sub"],
    "division": ["div"], "divide": ["div"], "quotient": ["div"],
    "divisible": ["dvd"], "divides": ["dvd"], "divisibility": ["dvd"],
    "inequality": ["le", "lt"], "less": ["le", "lt"], "greater": ["ge", "gt"],
    "equal": ["eq"], "equality": ["eq"], "equals": ["eq"],
    "monotone": ["mono", "le"], "monotonicity": ["mono", "le"], "monotonic": ["mono", "le"],
    "commutative": ["comm"], "commutativity": ["comm"], "commute": ["comm"],
    "associative": ["assoc"], "associativity": ["assoc"],
    "distributive": ["distrib"], "distributivity": ["distrib"],
    "natural": ["nat"], "naturals": ["nat"], "integer": ["int"], "integers": ["int"],
    "rational": ["rat"], "rationals": ["rat"], "reals": ["real"],
    "power": ["pow"], "exponent": ["pow"], "exponential": ["exp", "pow"],
    "root": ["sqrt"], "factorial": ["factorial"],
    "absolute": ["abs"], "minimum": ["min"], "maximum": ["max"],
    "modulo": ["mod", "emod"], "remainder": ["mod", "emod"], "modular": ["mod", "modeq"],
    "cardinality": ["card"], "size": ["card"], "count": ["card", "count"],
    "nonnegative": ["nonneg"], "positive": ["pos"], "negative": ["neg"],
    "injective": ["injective"], "surjective": ["surjective"], "bijective": ["bijective"],
    "even": ["even"], "odd": ["odd"], "coprime": ["coprime"],
}

STOPWORDS = {"of", "the", "for", "and", "with", "that", "this", "number", "numbers",
             "lemma", "theorem", "about", "between", "over", "under", "are", "is"}


def sanitize(q: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_.']+", q)
    toks = []
    for w in words:
        toks += re.split(r"[._]", w)
        toks.append(w.replace("'", ""))
    toks = [t.lower() for t in toks if len(t) > 1]
    toks = [t for t in toks if t not in STOPWORDS]
    expanded = list(toks)
    for t in toks:
        expanded += MATH_SYNONYMS.get(t, [])
    return list(dict.fromkeys(expanded))[:16]


@mcp.tool()
async def mathlib_search(query: str, k: int = 12) -> str:
    """
    Look up specific Mathlib declarations by name, signature fragment, or
    hypothesis pattern -- e.g. "monotonicity of natural number multiplication",
    "Cauchy-Schwarz inequality", or a bare name fragment after an "Unknown
    constant" / "Unknown identifier" compiler error.

    Mathlib does NOT contain the solution to your overall problem, so do not
    use this to "find the proof" -- use it only to resolve a specific lemma
    you already know you need. Returns name + kind + signature + docstring +
    module for each match, ranked by relevance.
    """
    def run_query():
        toks = sanitize(query)
        if not toks:
            return []
        limit = max(1, min(k, 30))

        def run(match: str, lim: int):
            try:
                return con.execute(
                    "SELECT name, kind, signature, docstring, module, rank"
                    " FROM decls WHERE decls MATCH ? ORDER BY rank LIMIT ?",
                    (match, lim),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

        quoted = [f'"{t}"' for t in toks]
        rows = run(" AND ".join(quoted), limit)
        if len(rows) < limit:
            seen = {r["name"] for r in rows}
            rows += [r for r in run(" OR ".join(quoted), limit * 2) if r["name"] not in seen][: limit - len(rows)]
        return rows[:limit]

    logger.info(f"mathlib_search query: {query!r}")
    rows = await asyncio.to_thread(run_query)
    if not rows:
        return ("No Mathlib declarations matched. Try different keywords, or "
                "compile `example : <goal> := by exact?` instead.")
    return "\n".join(
        f"{r['kind']} {r['name']} : {r['signature']}" + (f"\n    -- {r['docstring']}" if r["docstring"] else "")
        for r in rows
    )


async def main_serve():
    n = con.execute("SELECT count(*) c FROM decls").fetchone()["c"]
    logger.info(f"Leak XI booting — {n} declarations indexed.")

    http_app = mcp.sse_app()
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*", "mcp-protocol-version", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )

    logger.info("Serving Leak XI MCP (SSE) on 0.0.0.0:7860")
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

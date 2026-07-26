"""Leak XI — `mathlib_search`, the retrieval tool of the Architect stack.

Contract (paper C.2): a lookup helper for *specific* Mathlib lemmas — a
name, a signature fragment, a hypothesis pattern ("monotonicity of natural
number addition", "Cauchy-Schwarz inequality"), or recovering the right
name after an "Unknown constant" error. Returns compact, citeable results:
name + kind + signature + docstring + module.

Query handling: FTS5 lexical match over names (with camelCase/snake_case
subtoken expansion), signatures, and docstrings; graceful fallback from
strict AND to OR matching so near-miss queries still return candidates.
"""

import os
import re
import sqlite3

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/opt/index/mathlib.db")

app = FastAPI(title="Leak XI", version="1.0")

# Optional shared-secret gate. When LEAK_SERVICE_TOKEN is set, every request
# except /health must carry `Authorization: Bearer <token>`. These services
# compile arbitrary Lean, which can perform IO at elaboration time — so an
# open port without this is an unauthenticated code-execution surface.
SERVICE_TOKEN = os.environ.get("LEAK_SERVICE_TOKEN", "")


@app.middleware("http")
async def _require_token(request, call_next):
    if SERVICE_TOKEN and request.url.path != "/health":
        if request.headers.get("authorization", "") != f"Bearer {SERVICE_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


con = sqlite3.connect(DB_PATH, check_same_thread=False)
con.row_factory = sqlite3.Row


class SearchReq(BaseModel):
    query: str
    k: int = 12


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


@app.get("/health")
def health():
    n = con.execute("SELECT count(*) c FROM decls").fetchone()["c"]
    return {"service": "leak-xi", "decls": n}


@app.post("/search")
def search(req: SearchReq):
    toks = sanitize(req.query)
    if not toks:
        return {"results": []}
    k = max(1, min(req.k, 30))

    def run(match: str, limit: int):
        try:
            return con.execute(
                "SELECT name, kind, signature, docstring, module, rank"
                " FROM decls WHERE decls MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    quoted = [f'"{t}"' for t in toks]
    rows = run(" AND ".join(quoted), k)
    if len(rows) < k:
        seen = {r["name"] for r in rows}
        rows += [r for r in run(" OR ".join(quoted), k * 2) if r["name"] not in seen][: k - len(rows)]

    return {
        "results": [
            {
                "name": r["name"],
                "kind": r["kind"],
                "signature": r["signature"],
                "docstring": r["docstring"],
                "module": r["module"],
            }
            for r in rows[:k]
        ]
    }

"""Leak XI index builder — runs at Docker build time.

Extracts every public declaration (theorem/lemma/def/abbrev/instance/
structure/inductive) from the Mathlib source tree into a SQLite FTS5 index:
name, kind, signature, docstring, module. The paper's `mathlib_search` is a
*lookup helper* for specific lemmas — name / signature / hypothesis-pattern
queries — so a lexical FTS index with camelCase/snake_case-aware
tokenisation covers the contract without a GPU or an embedding server.

(An optional embedding rerank can be layered on later; the server exposes
the same /search contract either way.)
"""

import os
import re
import sqlite3
import sys

MATHLIB_DIR = sys.argv[1] if len(sys.argv) > 1 else "/opt/mathlib4"
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else "/opt/index/mathlib.db"

DECL_RE = re.compile(
    r"(?P<doc>/--(?:[^-]|-(?!/))*?-/\s*)?"
    r"^(?P<mods>(?:@\[[^\]]*\]\s*)*(?:protected\s+|private\s+|noncomputable\s+|scoped\s+)*)"
    r"(?P<kind>theorem|lemma|def|abbrev|instance|structure|inductive|class)\s+"
    r"(?P<name>[A-Za-z_«][A-Za-z0-9_'.«»]*)"
    r"(?P<sig>[\s\S]{0,600}?)(?::=|\bwhere\b|\n\s*\|)",
    re.M,
)


def subtokens(name: str) -> str:
    """mul_le_mul_left / Finset.sum_comm -> searchable word soup."""
    parts = re.split(r"[._]", name)
    words = []
    for p in parts:
        words.append(p)
        words += re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", p)
    return " ".join(dict.fromkeys(w.lower() for w in words if w))


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE VIRTUAL TABLE decls USING fts5(
          name, name_tokens, kind UNINDEXED, signature, docstring, module UNINDEXED,
          tokenize = 'porter unicode61'
        );
        """
    )
    count = 0
    for root, _dirs, files in os.walk(os.path.join(MATHLIB_DIR, "Mathlib")):
        for f in files:
            if not f.endswith(".lean"):
                continue
            path = os.path.join(root, f)
            module = os.path.relpath(path, MATHLIB_DIR)[:-5].replace(os.sep, ".")
            try:
                src = open(path, encoding="utf-8").read()
            except OSError:
                continue
            for m in DECL_RE.finditer(src):
                name = m.group("name")
                if name.startswith("_"):
                    continue
                sig = re.sub(r"\s+", " ", (m.group("sig") or "")).strip()
                doc = (m.group("doc") or "").strip()
                doc = re.sub(r"^/--\s*|\s*-/$", "", doc)
                doc = re.sub(r"\s+", " ", doc)[:600]
                con.execute(
                    "INSERT INTO decls VALUES (?,?,?,?,?,?)",
                    (name, subtokens(name), m.group("kind"), sig[:600], doc, module),
                )
                count += 1
    con.commit()
    con.execute("INSERT INTO decls(decls) VALUES('optimize')")
    con.commit()
    con.close()
    print(f"indexed {count} declarations -> {DB_PATH}")


if __name__ == "__main__":
    main()

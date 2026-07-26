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


NS_OPEN = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.\u00c0-\uffff]*)", re.M)
NS_END = re.compile(r"^\s*end\s*([A-Za-z_][A-Za-z0-9_'.\u00c0-\uffff]*)?\s*$", re.M)


def namespace_spans(src: str) -> list[tuple[int, str]]:
    """[(char_offset, dotted_namespace_prefix)] checkpoints, in order.

    Tracks `namespace X ... end X` nesting. A bare `end` (or an `end` naming
    something other than the innermost namespace) closes a `section`, not a
    namespace, so it is ignored — matching Lean's own scoping.
    """
    events = []
    for m in NS_OPEN.finditer(src):
        events.append((m.start(), "open", m.group(1)))
    for m in NS_END.finditer(src):
        events.append((m.start(), "end", m.group(1)))
    events.sort()
    stack: list[str] = []
    spans: list[tuple[int, str]] = [(0, "")]
    for pos, kind, nm in events:
        if kind == "open":
            stack.append(nm)
        elif stack and nm and (stack[-1] == nm or ".".join(stack).endswith(nm)):
            stack.pop()
        spans.append((pos, ".".join(stack)))
    return spans


def namespace_at(spans: list[tuple[int, str]], pos: int) -> str:
    lo, hi = 0, len(spans) - 1
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        if spans[mid][0] <= pos:
            best = spans[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


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
            ns_spans = namespace_spans(src)
            for m in DECL_RE.finditer(src):
                name = m.group("name")
                if name.startswith("_"):
                    continue
                # Qualify with the enclosing `namespace` blocks: Mathlib
                # declares `theorem mul_le_mul_left` inside `namespace Nat`,
                # and an agent citing the bare name writes an unknown
                # identifier. Retrieval must return what Lean will accept.
                prefix = namespace_at(ns_spans, m.start("name"))
                if prefix:
                    name = f"{prefix}.{name}"
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

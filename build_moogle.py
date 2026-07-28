"""Leak XI — moogle index builder. Runs at Docker build time.

Builds the SEMANTIC half of Leak XI: a Chroma collection of Mathlib
declarations embedded by their docstrings, so an agent that knows a concept
in English ("difference of squares") can discover the Lean name.

Two things make this different from Leak-I's moogle, and both matter:

1. It is built from the SAME source tree loogle is compiled against — the
   packages lake resolved inside this image — instead of a prebuilt index
   committed to the repo. moogle can therefore never suggest a name that
   loogle (and the prover's Lean) does not have. A retrieval tool whose two
   halves disagree about which Mathlib exists is worse than no tool.

2. It indexes Lean core and Batteries as well as Mathlib. The previous FTS
   index walked `<mathlib>/Mathlib` only, so the entire core `Nat`/`Int`/
   `List` API was invisible — `Nat.mul_div_assoc` and `Nat.dvd_of_mod_eq_zero`
   both exist and both returned nothing but unrelated near-neighbours, which
   is exactly how a prover ends up inventing names.

Only declarations WITH a docstring are embedded: the docstring is the thing
being searched semantically, and a name with no prose contributes noise, not
recall. Name/signature lookup is loogle's job, and loogle indexes everything.
"""

import glob
import os
import re
import sys

DECL_RE = re.compile(
    r"(?P<doc>/--(?:[^-]|-(?!/))*?-/\s*)?"
    r"^(?P<mods>(?:@\[[^\]]*\]\s*)*(?:protected\s+|private\s+|noncomputable\s+|scoped\s+)*)"
    r"(?P<kind>theorem|lemma|def|abbrev|instance|structure|inductive|class)\s+"
    r"(?P<name>[A-Za-z_«][A-Za-z0-9_'.«»]*)"
    r"(?P<sig>[\s\S]{0,600}?)(?::=|\bwhere\b|\n\s*\|)",
    re.M,
)

NS_OPEN = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.À-￿]*)", re.M)
NS_END = re.compile(r"^\s*end\s*([A-Za-z_][A-Za-z0-9_'.À-￿]*)?\s*$", re.M)


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


def qualify(prefix: str, name: str) -> str:
    """Prefix a declaration with its enclosing namespaces, without doubling.

    Mathlib writes `theorem filter_false` inside `namespace Finset`, so the
    prefix is required — an agent citing the bare name writes an unknown
    identifier. But it also writes `theorem Finset.filter_mem_eq_inter` at
    top level, and some files reopen a namespace in a way that leaves the
    same component on the stack twice. Blind prefixing produced real hits
    named `Finset.Finset.filter_false`, which is worse than no prefix: it
    is a name that cannot exist. Only add the components that are missing.
    """
    if not prefix:
        return name
    if name.startswith(prefix + "."):
        return name
    parts = prefix.split(".")
    # Drop any leading prefix components the name already carries.
    while parts and name.startswith(parts[-1] + "."):
        parts.pop()
    return ".".join(parts + [name]) if parts else name


def clean_sig(sig: str) -> str:
    """The capture starts right after the name, so it usually begins with the
    binders and then `:`. Collapse whitespace and drop a leading colon so the
    rendered line reads `theorem foo (n : ℕ) : P n`, not `theorem foo : : P n`.
    """
    s = re.sub(r"\s+", " ", sig or "").strip()
    return re.sub(r"^:\s*", "", s) if s.startswith(":") and not s.startswith(":=") else s


def iter_decls(roots: list[tuple[str, str]]):
    """roots = [(base_dir, subdir)]; module names are relative to base_dir."""
    for base, sub in roots:
        top = os.path.join(base, sub)
        if not os.path.isdir(top):
            print(f"  ! missing source root, skipping: {top}", flush=True)
            continue
        n = 0
        for dirpath, _dirs, files in os.walk(top):
            for f in files:
                if not f.endswith(".lean"):
                    continue
                path = os.path.join(dirpath, f)
                module = os.path.relpath(path, base)[:-5].replace(os.sep, ".")
                try:
                    src = open(path, encoding="utf-8").read()
                except OSError:
                    continue
                spans = namespace_spans(src)
                for m in DECL_RE.finditer(src):
                    name = m.group("name")
                    if name.startswith("_"):
                        continue
                    doc = (m.group("doc") or "").strip()
                    if not doc:
                        continue
                    doc = re.sub(r"^/--\s*|\s*-/$", "", doc)
                    doc = re.sub(r"\s+", " ", doc).strip()[:600]
                    if len(doc) < 12:
                        continue
                    yield {
                        "name": qualify(namespace_at(spans, m.start("name")), name),
                        "kind": m.group("kind"),
                        "signature": clean_sig(m.group("sig"))[:600],
                        "docstring": doc,
                        "module": module,
                    }
                    n += 1
        print(f"  {sub}: {n} documented declarations", flush=True)


def source_roots(packages_dir: str) -> list[tuple[str, str]]:
    """Every tree that ends up in loogle's environment via `import Mathlib`."""
    roots = [
        (os.path.join(packages_dir, "mathlib"), "Mathlib"),
        (os.path.join(packages_dir, "batteries"), "Batteries"),
    ]
    # Lean core ships its sources inside the elan toolchain. The directory is
    # name-mangled per toolchain, so glob rather than hardcode.
    for core in sorted(glob.glob(os.path.expanduser("~/.elan/toolchains/*/src/lean"))):
        roots.append((core, "Init"))
        roots.append((core, "Std"))
    return roots


def main():
    packages_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/loogle/.lake/packages")
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "chroma_db")
    model_name = os.environ.get("MOOGLE_MODEL", "all-MiniLM-L6-v2")

    import chromadb
    from sentence_transformers import SentenceTransformer

    print(f"Extracting documented declarations from {packages_dir} …", flush=True)
    docs = list(iter_decls(source_roots(packages_dir)))
    # Same name in two modules (core states it, Mathlib restates it) — keep the
    # first, which is the earlier root in source_roots order.
    seen, uniq = set(), []
    for d in docs:
        if d["name"] in seen:
            continue
        seen.add(d["name"])
        uniq.append(d)
    print(f"{len(uniq)} unique documented declarations to embed.", flush=True)
    if not uniq:
        raise SystemExit("refusing to build an empty moogle index — check the source roots")

    os.makedirs(out_dir, exist_ok=True)
    client = chromadb.PersistentClient(path=out_dir)
    try:
        client.delete_collection("moogle")
    except Exception:
        pass
    coll = client.create_collection(name="moogle")

    model = SentenceTransformer(model_name)
    BATCH = 512
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i : i + BATCH]
        # Embed the docstring with the name's own words in front: Mathlib names
        # ARE prose ("mul_le_mul_left" -> "mul le mul left"), and including them
        # lets a query phrased in Mathlib's abbreviations hit too.
        texts = [f"{d['name'].replace('.', ' ').replace('_', ' ')}. {d['docstring']}" for d in chunk]
        coll.add(
            ids=[f"{i + j}" for j in range(len(chunk))],
            embeddings=model.encode(texts, batch_size=64, show_progress_bar=False).tolist(),
            documents=[d["docstring"] for d in chunk],
            metadatas=[
                {"name": d["name"], "kind": d["kind"], "signature": d["signature"], "module": d["module"]}
                for d in chunk
            ],
        )
        print(f"  embedded {min(i + BATCH, len(uniq))}/{len(uniq)}", flush=True)

    print(f"moogle index built at {out_dir} ({coll.count()} entries)", flush=True)


if __name__ == "__main__":
    main()

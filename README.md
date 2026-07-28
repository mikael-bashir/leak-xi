---
title: Leak XI
emoji: 🔍
colorFrom: indigo
colorTo: blue
sdk: docker
pinned: false
short_description: loogle + moogle Mathlib retrieval, Leak X toolchain
---

# Leak XI — Mathlib retrieval @ v4.32.0

Search for the Goedel-Architect pipeline (the `architect` strategies in
[nextjs-ai-chatbot](https://github.com/mikael-bashir/nextjs-ai-chatbot)).
Same architecture as **Leak-I** — a persistent `loogle -i --json` daemon plus a
Chroma/MiniLM semantic index over FastMCP/SSE — rebuilt against **v4.32.0**,
the toolchain **Leak XII** and **Leak XIV** elaborate with.

## Tools

| Tool | Question it answers | Can it say "no"? |
|---|---|---|
| `loogle_search` | "What is the real name / signature?" (Lean pattern or name) | **Yes** — zero hits is proof of absence |
| `moogle_search` | "What is this concept called in Mathlib?" (English) | No — always returns nearest neighbours |
| `mathlib_search` | compatibility router; picks one of the above by query shape | inherits whichever it used |

They are separate on purpose. `loogle_search` searches the *elaborated
environment*, so an empty result is evidence; `moogle_search` is a vector
index that always has a best guess. Merging them destroys the negative answer,
and the negative answer is the expensive one to be without.

## Why this replaced the FTS index

Measured against a real `floors_recover_whole` run on the previous
SQLite/FTS5 build:

- **No Lean core coverage.** The indexer walked `<mathlib>/Mathlib` only, so
  the core `Nat`/`Int`/`List` API was invisible. `Nat.mul_div_assoc` closed a
  node in that run, and searching for it returned five unrelated declarations
  with no hint that the real one simply was not indexed. loogle indexes the
  environment, so `import Mathlib` coverage is automatic.
- **It could not say no.** Every query fell back from AND to OR matching, so
  `Nat.div_eq_zero_of_dvd` — which does not exist — came back with
  `Nat.maxPowDvdDiv_zero_left` and friends. The prover read that as a
  near-miss and spent most of a ten-minute run on variants of a name that was
  never there.

## Build notes

- Mathlib is pinned to the **release tag** matching the toolchain, not
  `master`; both the toolchain write and the tag pin are asserted, so a
  silent upstream change fails the build instead of shipping a server that
  answers from the wrong library.
- `LOOGLE_REV` is pinned to `ba48a42e` — upstream's most recent commit that
  still declares a Mathlib dependency. loogle `master` has no `require
  mathlib` and an empty manifest; it builds fine and indexes nothing.
- The moogle index is built at image-build time from the packages lake just
  resolved (Mathlib + Batteries + Lean core), so the semantic half can never
  suggest a name the exact half does not have. It is non-fatal: if it fails,
  `loogle_search` still serves and `moogle_search` says so.

---
title: Leak XI
emoji: 🔍
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 8011
pinned: false
---

# Leak XI — `mathlib_search`

Retrieval tool for the Goedel-Architect pipeline (see the `architect`
strategy in [nextjs-ai-chatbot](https://github.com/mikael-bashir/nextjs-ai-chatbot)).
SQLite FTS5 index over Mathlib v4.32.0 declarations, subtoken-aware
(`Nat.mul_le_mul_left` matches "monotonicity of multiplication").

Set the `LEAK_SERVICE_TOKEN` Space secret to require
`Authorization: Bearer <token>` on every request except `/health`.

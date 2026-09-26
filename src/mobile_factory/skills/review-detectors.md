---
description: Review part — triage the deterministic detector findings (runs in parallel with correctness and taste, read-only).
---

# Review part: detectors

Inputs in `<run>/context/`: `detector.json` (AI-slop findings with file:line), `diff.patch`.
Output: `{"findings": [...]}` in the `factory schema review` shape.

Copy every detector finding (same rule, file, line, severity, `source: "detector"`). Look at the line: a real hit stays
`open`; a false positive becomes `dismissed` with a one-line `reason`. Add nothing else.

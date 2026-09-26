---
description: Review part — owner taste and tribal-knowledge rules (runs in parallel with correctness and detectors, read-only).
---

# Review part: taste

Inputs in `<run>/context/`: `diff.patch`, `rules.md` (owner taste rules + tribal knowledge, with "Accepted exceptions").
Output: `{"findings": [...]}` in the `factory schema review` shape, every `outcome` left `open`.

Added/changed lines only. Every finding cites a rule id from `rules.md`; a finding that matches an Accepted exception is
dropped. Also flag what the owner would reject even without a rule (rule `GENERAL`, max 3, at most `major`): scope creep,
unrelated edits, a second way of doing what the codebase already does one way, needless abstractions, defensive null
checks the types rule out, missing test for testable logic, naming against the local convention, logs that leak data.
Max 10 findings, most severe first.

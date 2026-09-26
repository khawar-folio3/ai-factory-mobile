---
description: Review part — bugs the change introduces (runs in parallel with taste and detectors, read-only).
---

# Review part: correctness

Inputs in `<run>/context/`: `diff.patch`. Read surrounding source only to confirm a finding.
Output: `{"findings": [...]}` in the `factory schema review` shape, every `outcome` left `open`.

Added/changed lines only. Look for what breaks at runtime: null/lifecycle misuse, leaked observers or coroutines,
wrong thread, off-by-one, state not restored on rotation/process death, missing error path, a behaviour change outside
the ticket. Rule `GENERAL`, max 5 findings, `blocker` only for a crash or data loss. No finding without `file:line`.

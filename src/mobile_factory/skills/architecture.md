---
description: Architecture — choose the stack and structure for a new app; one reason per decision.
---

# Architecture — decisions a lead signs off

Inputs: the spec output, and the team's existing apps (scout the sibling repos and taste rules for conventions).

- Prefer what the team already runs in production; a new library needs a reason in `why`.
- Decide: platform, modules, layering and state pattern, navigation, DI, networking, persistence, error handling,
  testing (unit/UI), CI, build variants, and `libraries` with pinned versions.
- Keep the first version small enough for the first slice; note what later slices will add.

A human approves the architecture before any code is written.

- Shell: cap command output (`| tail -40`, `head`, `sed -n 'a,bp'`), quote globs, read files by line range
  instead of whole files.

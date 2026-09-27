---
description: Scaffold — create the new project per the architecture and build the first slice with tests and CI.
---

# Scaffold — the project and its first slice

- Create the project exactly as the architecture says (modules, packages, build files, versions). Build must pass
  from a clean checkout; add the CI config the architecture names.
- Build the first slice from the spec so every acceptance criterion holds; unit tests for its logic.
- Strings in resources (all planned locales), theme tokens instead of raw colours, no secrets in code.
- Do not commit. `summary` is the commit subject without the ticket key: imperative, ≤ 72 chars.

---
description: Scaffold — create the new project per the architecture and build the first slice with tests and CI.
---

# Scaffold — the project and its first slice

- Create the project exactly as the architecture says (modules, packages, build files, versions). Build must pass
  from a clean checkout; add the CI config the architecture names.
- Build the first slice from the spec so every acceptance criterion holds; unit tests for its logic.
- Strings in resources (all planned locales), theme tokens instead of raw colours, no secrets in code.
- Prove every acceptance criterion of the first slice on the device: one Maestro flow at the FLOW line with a
  `# criterion N: <text>` section each, run with `maestro --device <serial> test <flow>` (at most 3 fix rounds).
- Do not commit. JSON as the work step's: `summary` (commit subject without the ticket key, imperative, ≤ 72 chars),
  `acceptance_criteria` (each `met` + `evidence`, or `blocked` + `reason`), `flow`, `notes`.
- Shell: cap command output (`| tail -40`, `head`, `sed -n 'a,bp'`), quote globs, read files by line range
  instead of whole files.

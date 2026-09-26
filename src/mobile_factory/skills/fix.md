# Fix — the smallest change that removes the root cause

Output: `factory schema fix`. Do not commit; the runner commits after verification.

- Start from the triage hypothesis, the reproduction and, when the HINT line names it, `context/locate.json` (files,
  call sites, tests mapped while reproduce ran). Confirm the root cause in code before editing.
- If a LAST FAILURE line is shown, you are on a retry: read the failing log tail it names and fix that, don't start over.
- Follow the surrounding code: naming, patterns, error handling, comment density. Reuse existing helpers.
- Add or adjust a unit test when the logic is testable (pure functions, mappers, view-model state). Say so in `tests_added`.
- Not allowed (the runner stops the run on these): forbidden paths, build config, `@Suppress` / `tools:ignore` /
  `swiftlint:disable` / `@Ignore`, deleting or disabling tests, new dependencies, public API or DB schema changes,
  unrelated formatting.
- Clean as you go, the guardrail will flag it anyway: no narrating comments ("// Step 1", "// Added ..."), no
  commented-out code, no debug prints, no `!!` / force unwraps, no swallowed exceptions, no new doc comments on
  internal code, no speculative abstractions.

`summary` is the commit subject without the ticket key: imperative, ≤ 72 chars ("Wrap avatar height on profile header").
`changes`: what changed, file by file, and why this is the smallest fix.

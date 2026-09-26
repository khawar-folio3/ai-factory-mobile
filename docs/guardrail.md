# The guardrail

Goal: a PR that the code owner would approve on the first pass, and no AI fingerprints on it.

## Layers, cheapest first

1. **Hard limits** — `guardrail/limits.py`. Forbidden paths, suppressions, deleted tests. A hit stops the run at the
   fix step; nothing is silently reverted.
2. **AI-slop detectors** — `templates/slop.yaml`, regexes over *added* lines with file globs. Output has `file:line`,
   so the reviewer agent cannot hand-wave: every blocker/major needs an outcome.
3. **Owner taste** — `.factory/taste.md`, rules distilled from your code owners' review comments on merged PRs.
4. **Tribal knowledge** — `.factory/knowledge/*.md`, rules nobody wrote down yet: architecture boundaries, traps,
   "we tried that".
5. **Your own commands** — `guardrail.commands` (`./gradlew detekt`, `ktlintCheck`…) run in the checks step.

## Rule format (taste and knowledge)

```
### R012 · Prefer sealed UI state over nullable flags   [major]
applies: **/ui/**/*.kt, **/*ViewModel.kt
keywords: Boolean|isLoading|: String\?
why: owner rejects boolean/nullable state combos; wants exhaustive `when`.
bad:  var isLoading: Boolean; var error: String?
good: sealed interface UiState { Loading; Error(msg); Content(data) }
evidence: 7 comments · PRs #412 #430 #455
```

- A rule loads only if `applies` matches a changed file **and** (when present) `keywords` hits an added line or path,
  or `keywords-removed` hits a removed line. Rules without keywords always load when the glob matches.
- Sections after the rules (`## Accepted exceptions`, `## How this reviewer works`…) always load.
- Ids are permanent. Severity: `blocker` (owner requested changes for it), `major`, `nit`.

## Learning the owner's taste

```bash
factory guardrail learn --bases develop --limit 300
```

Harvests inline and review-body comments by the code owners (CODEOWNERS, else the top reviewers) from merged PRs,
with thread resolution (did the code change? did the author push back and win?). Output goes to
`.factory/data/reviews.jsonl` — git-ignored because it contains names. Then ask your agent to follow the
`guardrail-learn` skill: it reads the data in chunks, clusters by underlying preference, keeps rules with evidence and
writes `taste.md` without names. **The code owner approves `taste.md` in a PR** like any other code.

Refresh monthly with `--since <last harvest date>`; the skill merges evidence and marks stale rules.

## Outcomes

| Outcome | Meaning | Effect |
|---|---|---|
| `applied` | code changed | checks + verify + commit (amend) re-run, then another review round |
| `not applied` + reason | real issue, not safely fixable | blocker/major → run stops (`gate-needs-work`) |
| `dismissed` + reason | detector false positive | blocker/major → the review gate goes to a human at any level |

The last round must apply nothing (`limits.max_review_rounds`, default 2), so what the human sees is what was reviewed.

## Using it without a run

```bash
factory guardrail review --base develop     # context for the current branch in .factory/data/review/
factory guardrail check findings.json       # validate findings and print the verdict table
```

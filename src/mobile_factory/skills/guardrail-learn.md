# Guardrail learn — distill the code owner's taste from past PR reviews

Run `factory guardrail learn [--bases develop,release/x] [--since YYYY-MM-DD]` first. It writes
`.factory/data/reviews.jsonl` (one owner comment per line, git-ignored: it contains names) and prints counts.
You turn that into `.factory/taste.md` (committed, reviewed by the owner like code).

Each comment: `{id, pr, title, pr_author, merged_at, kind: inline|review, reviewer, state, path, line, diff_hunk, body, resolution}`.

## 1. Read lean

Never read the whole file. Project and chunk (~60 comments at a time):
```sh
jq -c '{pr, state, path, body: (.body[:400]), hunk: (.diff_hunk | split("\n") | .[-4:] | join("\n"))}' .factory/data/reviews.jsonl | sed -n '1,60p'
```
Keep a tally per chunk (`candidate rule → PR numbers`), not the comments.

## 2. Drop

Questions with no preference, thanks, merge logistics, one-off product corrections, patterns no longer in the code
(grep before keeping a rule that names an API), anything that identifies a person.

## 3. Weigh the resolution

A comment is evidence only if the owner's position held: `outdated: true` or the author agreed → counts.
Author pushed back and the owner conceded → not evidence (record it under Accepted exceptions if it recurs).
"ok for now / next time" with no change → counts, at most `major`.

## 4. Cluster and keep

Group by the underlying preference, not the wording. Keep a rule with ≥ 2 comments, or 1 inside a `CHANGES_REQUESTED`
review. Cap 60. Severity: `blocker` if the owner requested changes for it at least once; `major` if repeated (≥ 3)
or about correctness, crashes, leaks, security, architecture; else `nit`.

## 5. Write `.factory/taste.md` (≤ ~4k tokens)

Header: repo, harvest date, PR window, comment count. No names. Then:
```
### R012 · Prefer sealed UI state over nullable flags   [major]
applies: **/ui/**/*.kt, **/*ViewModel.kt
keywords: Boolean|isLoading|: String\?
why: owner rejects boolean/nullable state combos; wants exhaustive `when`.
bad:  var isLoading: Boolean; var error: String?
good: sealed interface UiState { Loading; Error(msg); Content(data) }
evidence: 7 comments · PRs #412 #430 #455
```
- IDs `R001…` are stable forever; never renumber, never reuse.
- `applies`: globs from the evidence paths (`**/` any depth, `*` one segment).
- `keywords` / `keywords-removed` (optional regex on added / removed lines): the rule loads only on a hit. Omit for
  judgement rules (scope, simplicity, testing) so they always load when the glob matches.
- Then three short sections, always loaded: `## Accepted exceptions`, `## Seen once, not rules`, `## How this reviewer works`.

## 6. Refresh (`--since <last harvest date>`)

Matching comment → bump evidence, prepend PR numbers (keep 5). New cluster meeting the threshold → next free id.
New `CHANGES_REQUESTED` hit → raise to `blocker`. Newest evidence older than 6 months → append `[stale]` to the title.

Finish by showing the rule count per severity and one line per rule, and **stop for code-owner approval**.

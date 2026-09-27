---
description: Taste distill part — tally one line range of the harvested reviews into candidate rules (runs in parallel, read-only).
---

# Learn part: tally one chunk

Input: a line range of `<data>/reviews.jsonl` (both given in your prompt). Output: the `tally-<n>.json` path in your prompt.

Read only your lines, projected lean:
```sh
sed -n '<a>,<b>p' <data>/reviews.jsonl | jq -c '{pr, state, path, resolution, body: (.body[:400]), hunk: (.diff_hunk | split("\n") | .[-4:] | join("\n"))}'
```
Apply steps 2-3 of the guardrail-learn skill (drop noise, weigh the resolution). Write your output file with the
Write tool (never `cat >`/heredocs: headless runs refuse unlisted shell commands):
```json
{"chunk": 1, "candidates": [
  {"preference": "prefer sealed UI state over nullable flags", "paths": ["feature/x/ui/FooViewModel.kt"],
   "keywords": "Boolean|isLoading", "prs": [412, 430], "comments": 3, "changes_requested": true, "conceded": 0,
   "example": {"bad": "var isLoading: Boolean", "good": "sealed interface UiState"}}
]}
```
One candidate per underlying preference in your chunk (not per comment). No names. No rule ids: the merge pass assigns them.

- Shell: cap command output (`| tail -40`, `head`, `sed -n 'a,bp'`), quote globs, read files by line range
  instead of whole files.

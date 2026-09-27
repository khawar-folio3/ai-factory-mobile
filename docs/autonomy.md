# Autonomy and gates

## The dial

`autonomy.ceiling` (0–4) is what the team is willing to allow. The **risk score** is what this change deserves.
The run uses the lower of the two, and re-scores after the fix using the actual diff — a "one-file fix" that turned
into eight files loses its autonomy mid-run.

## Risk score

| Signal | Points |
|---|---|
| files (expected after triage, actual after fix) | 3 per file, max 30 |
| modules touched beyond the first | 10 each, max 20 |
| lines changed > 50 / > 200 | 5 / 15 |
| public API / contract change | 20 |
| high-risk area (`autonomy.high_risk_classes`: auth, payment, security, data_loss, migration, mdm, crypto) | 40 |
| not reproduced / confidence < 0.5 | 25 / 15 |
| more than one platform | 15 |
| low-risk category (`autonomy.low_risk_categories`: typo, copy, ui_spacing, color_token…) | −10 |

`autonomy.thresholds` maps a level to the highest score it allows (default `{4: 15, 3: 35, 2: 55, 1: 75}`).

```
$ factory status
risk 46/100 -> allows L2, ceiling L4 => autonomy L2 (supervised: plan and repro are automatic; diff, review and PR ask)
    +6  2 file(s) expected
   +40  high-risk area: auth
```

## Gates

bugfix, task and feature only have the `pr` gate. The others are used by new-app and your own workflows.

| Gate | After | Default `auto_at` | The human sees |
|---|---|---|---|
| plan | triage | 1 | plan, root-cause guess, risk breakdown |
| repro | reproduce | 2 | steps, confidence, before snapshots |
| diff | fix | 3 | diff stat, re-scored risk |
| review | review | 3 | findings table with outcomes |
| pr | PR preview | 4 | exact title and body, hash, tracker transition |

A gate is automatic when `level >= auto_at`. Set `auto_at: 5` to make a gate always human.
Overrides that always win: a *dismissed* or open blocker/major finding forces the review and PR gates; nothing is ever merged or
marked ready for review.

## Suggested rollout

1. Weeks 1–2: ceiling 0–1. Every gate is a teaching moment; tune `taste.md` and `knowledge/`.
2. Replay 20–30 past tickets with `factory eval`. Raise the ceiling for the categories that score well.
3. Ceiling 3 for the team; 4 only for repos with good eval numbers and a PR reviewer on rotation.

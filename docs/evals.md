# Evals: prove it on tickets you already fixed

Before you raise a team's autonomy ceiling, replay history.

```bash
factory eval add APP-812 --fix-commit 4f2c1e9     # the squashed commit that fixed it
factory eval prepare APP-812                      # worktree at the parent commit, today's config copied in
cd .factory/evals/work/APP-812
factory run APP-812 --base develop --autonomy 4   # let the agent run it
factory eval score APP-812                        # compare with the human fix
factory eval report
```

Scored per case: outcome, reached PR, **file precision/recall** vs the human fix, fix attempts, review rounds, human
gates, risk and level. Results append to `.factory/evals/results.jsonl` in the main checkout.

## Picking cases

- 20–30 closed bugs from the last two quarters, one squashed commit each, mixed categories.
- Include a few that should be **ineligible** (backend, design change): a good run stops early with the right verdict.
- Keep the ticket text as it was when the bug was reported (use `tracker.kind: file` if the ticket was edited later).

## Reading the numbers

- Recall ≈ 1 with precision < 1: the agent found the spot but touched extra files → tighten `taste.md` on scope.
- Low recall: wrong root cause → look at the triage/reproduce outputs in the run folder.
- Many human gates at a given ceiling: the risk score is conservative for this repo; adjust
  `autonomy.low_risk_categories` or thresholds, not the ceiling.

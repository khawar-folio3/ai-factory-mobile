# Events and visualisation

Every state change appends one JSON line to `.factory/events.jsonl`:

```json
{"ts": "2026-09-26T10:31:02+00:00", "type": "gate.waiting", "run": "APP-1-20260926-103010", "gate": "diff", "ticket": "APP-1", "risk": 42, "level": 2, "summary": "Fix: Wrap avatar height"}
```

| type | fields |
|---|---|
| `run.started` | ticket, ceiling |
| `node.started` | node, title |
| `node.completed` | node, (next) |
| `node.failed` | node, reason, attempt |
| `agent.waiting` | node |
| `risk.assessed` | score, level, node |
| `gate.waiting` | gate, ticket, risk, level, summary |
| `gate.decided` | gate, decision (auto/approved/rejected), by |
| `checkpoint.rollback` | checkpoint, files |
| `run.finished` | ticket, outcome, reason, pr_url |

The schema is stable within a major version. `factory events -f` tails it.

## Sinks

- **Slack**: `notifications.slack_webhook: ${SLACK_WEBHOOK_URL}` posts when a gate waits and when a run finishes.
- **Pixel Agents**: `viz.pixel_agents: true`. See [viz/pixel-agents](../viz/pixel-agents/README.md).
- Anything else: tail the file. Sinks are best-effort and can never fail a run.

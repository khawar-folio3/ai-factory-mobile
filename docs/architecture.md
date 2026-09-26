# Architecture

## Principle: the agent thinks, the runner decides

Prompts are suggestions; a runner is a contract. Everything that must *always* happen — gates, limits, retries,
rollback, publishing — is Python code in `mobile_factory`, not text an LLM may skip. The agent (Claude Code, Cursor,
anything with a shell) is handed one step at a time and returns structured JSON.

```
          ┌──────────────────────────── factory CLI (this repo) ─────────────────────────────┐
agent ──▶ │ next / submit ──▶ Engine ──▶ auto nodes (git, gh, tracker, gradle, adb)           │
  ▲       │                     │  ├─▶ gates: autonomy.assess(risk) → auto | human approve   │
  │       │                     │  ├─▶ guardrail: limits + detectors + taste/knowledge rules │
  │       │                     │  └─▶ EventBus ─▶ events.jsonl ─▶ metrics · Slack · pixels  │
  └────── │ instructions (TASK, SKILL, LAST FAILURE, SUBMIT)                                  │
          └───────────────────────────────────────────────────────────────────────────────────┘
human ──▶ factory gate / approve / reject   (TTY only)
```

## Modules

| Module | Responsibility |
|---|---|
| `pipeline.py` | `Engine`: node graph, `advance()` / `submit()` / `decide()`, retries, rollback, publish guards |
| `outputs.py` | Pydantic schemas for each agent step (`factory schema <node>`) |
| `autonomy.py` | Risk signals → score → allowed level; gate thresholds |
| `state.py` | `RunState` persisted atomically to `.factory/runs/<id>/state.json`; resumable |
| `config.py` | `factory.yaml` schema, `${VAR}` interpolation, secrets file, plaintext-secret refusal |
| `guardrail/` | diff parsing, hard limits, rule matching (taste + knowledge), AI-slop detectors, review harvesting |
| `platforms/` | `Platform` interface; `android.py` (gradle, adb, uiautomator, Maestro) |
| `integrations/` | Tracker (Jira REST, twg CLI, Markdown files), GitHub via `gh` |
| `adapters.py` | Renders skills + MCP config for Claude Code and Cursor |
| `events.py` | JSONL event log and sinks (Slack webhook, Pixel Agents) |
| `evals.py`, `metrics.py` | Measurement |
| `skills/*.md` | The agent-facing instructions per step (portable Markdown) |

## Nodes (bugfix pipeline)

| Node | Kind | Gate | What happens |
|---|---|---|---|
| preflight | auto | | clean tree, base branch fetched, `gh` authenticated |
| intake | auto | | ticket fetched and trimmed to `ticket.json`; type/label/project filters |
| triage | agent | plan | eligibility verdict, root-cause hypothesis, plan → first risk score |
| branch | auto | | branch from `origin/<base>`, **checkpoint** recorded, optional tracker transition |
| reproduce | agent | repro | device snapshots of the faulty state and adjacent screens |
| fix | agent | diff | code change; hard limits; risk re-scored from the real diff |
| checks | auto | | lint + unit tests of touched modules, extra commands, build + install |
| verify | agent | | same snapshots after the fix; defect changed, neighbours unchanged |
| commit | auto | | one commit (amended on later rounds); guardrail context prepared |
| review | agent | review | findings vs taste/knowledge/detectors; applied fixes loop back to checks |
| pr_preview | auto | pr | PR body rendered, publish guards checked, preview hashed |
| publish | auto | | refuses unless the approved preview hash still matches; push + draft PR |
| handoff | auto | | outcome recorded |

A failed check or verification sends the run back to `fix` with the failure attached. After
`limits.max_fix_attempts` extra attempts the working tree is rolled back to the checkpoint (the failed diff is kept as
a patch in the run folder) and the run stops.

## Trust boundaries

- Ticket text, comments, PR comments and web pages are data. Skills say so; the runner never executes content from them.
- Approving a gate requires an interactive terminal and a one-time code. This is a speed bump against an agent
  approving itself, not a security boundary against a malicious local user.
- Every outbound write (push, PR, tracker transition) happens in `publish`/`branch` code paths only, after the gates.
- Secret-like files are stripped from any diff before an agent reads it.

## Extending

New pipeline: add a list of `Node`s to `PIPELINES` plus `AUTO`/`POST` handlers and an output model.
New platform: implement `Platform` (see [platforms.md](platforms.md)).

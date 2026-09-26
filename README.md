# Mobile Factory

**A human-in-the-loop AI factory for mobile teams.** It takes a bug ticket to a *verified* draft PR: reproduced on a
device, fixed, re-verified on the same screens, reviewed against your code owner's taste — with an autonomy dial that
decides how often a human is asked.

Works with **Claude Code** and **Cursor** (and any agent that can run a shell), because the pipeline lives in a small
Python runner, not in a prompt.

```
ticket ─▶ triage ─◆plan─▶ branch ─▶ reproduce ─◆repro─▶ fix ─◆diff─▶ checks ─▶ verify ─▶ commit ─▶ review ─◆review─▶ PR preview ─◆pr─▶ draft PR
                                       ▲  device                 ▲        lint/tests  device              guardrail
                                       │                          └──── retry with the failure (N times), then roll back ◀──┘
◆ = gate: automatic or human, decided per run by risk score × autonomy ceiling
```

> Status: **alpha (v0.1)**. Android is supported end to end. iOS is on the roadmap behind the same `Platform` interface.

## Why

AI agents write plausible code fast. What costs a team time is everything around it: *is this really the bug? does it
work on the device? did it break the screen next door? would our reviewer accept this?* Mobile Factory makes those
questions steps with evidence, and lets you choose — per run — how many of them a human signs off.

| | Typical "AI fixes the ticket" | Mobile Factory |
|---|---|---|
| Proof of the bug | none | `before` snapshots on a device, with diffable screen state |
| Proof of the fix | "tests pass" | same snapshots `after` + adjacent screens `unchanged` |
| Human control | all or nothing | 5 gates × autonomy 0–4, re-scored from the real diff |
| Code review | generic LLM review | rules learned from *your* merged PR reviews + tribal knowledge + deterministic AI-slop detectors |
| Failure mode | a messy branch | bounded retries, then automatic rollback to a checkpoint |
| Evidence for management | anecdotes | event log, metrics, and an eval harness replaying past tickets |

## Install

```bash
pipx install mobile-factory        # or: uv tool install mobile-factory
```

Requires Python 3.11+, `git`, `gh` (authenticated), and for Android: `adb`, a JDK and the project's `./gradlew`.
[Maestro](https://maestro.mobile.dev) is optional for multi-step flows.

## Quick start

```bash
cd your-android-app
factory init                          # guided: asks and verifies Jira site + sign-in, GitHub account, Slack, Figma; installs tools
factory doctor                        # tools, logins (and which account acts for you), device
factory install --target all          # skills + MCP config for Claude Code (.claude/, .mcp.json) and Cursor (.cursor/)
```

A repo set up once by the lead needs only `factory setup` and `factory doctor` on each new machine: see
[docs/onboarding.md](docs/onboarding.md).

Then ask the agent: *"fix APP-123 with the factory"*. The agent loops `factory next` → does the step → `factory submit`.
When a gate needs you, it stops and tells you to run, in your own terminal:

```bash
factory gate            # what you are approving
factory approve plan    # type the shown code to confirm (refused without a TTY, so the agent cannot self-approve)
```

No Jira? Set `tracker.kind: file` and write tickets as Markdown in `.factory/tickets/APP-123.md`. This is also the
easiest way for a PM to hand a small change to the factory.

## Autonomy

Every run has a **ceiling** (config or `--autonomy N`). After triage, and again after the fix, the runner computes a
**risk score** (0–100) from files, modules, lines, public API changes, high-risk areas (auth, payment, migrations…),
reproduction confidence and missing tests. The run's level is `min(ceiling, level the risk allows)`.

| Level | plan | repro | diff | review | pr |
|---|---|---|---|---|---|
| 0 manual | 🧍 | 🧍 | 🧍 | 🧍 | 🧍 |
| 1 assisted | auto | 🧍 | 🧍 | 🧍 | 🧍 |
| 2 supervised | auto | auto | 🧍 | 🧍 | 🧍 |
| 3 trusted | auto | auto | auto | auto | 🧍 |
| 4 autonomous | auto | auto | auto | auto | auto → **draft** PR |

A copy fix in one file scores ~0 and can run hands-off; a change touching auth scores ≥ 40 and drops to supervised no
matter the ceiling. `factory risk` explains every point. A reviewer's *dismissed* blocker always gets a human, at any
level. Nothing is ever merged or marked ready. Details: [docs/autonomy.md](docs/autonomy.md).

## The guardrail

The review step is a gate, not a suggestion:

1. **Hard limits** (deterministic): forbidden paths (build config, signing, CI), suppressions (`@Suppress`,
   `tools:ignore`, `swiftlint:disable`…), deleted tests. Any hit stops the run.
2. **AI-slop detectors** (deterministic, on added lines): narrating comments, commented-out code, debug prints, `!!`,
   force unwraps, swallowed exceptions, `GlobalScope`, `Thread.sleep`, hardcoded strings… Every blocker/major must be
   answered: applied, not applied (stops the run) or dismissed (forces a human).
3. **Owner taste**: `factory guardrail learn` harvests your code owners' comments from merged PRs; the agent distills
   them into `.factory/taste.md` — rules with ids, globs, keywords and PR evidence, approved by the owner like code.
4. **Tribal knowledge**: `.factory/knowledge/*.md`, same format, hand-written.

Only the rules whose globs/keywords match the diff are loaded, so review stays cheap. Details: [docs/guardrail.md](docs/guardrail.md).

## One config, every machine

`factory.yaml` is committed and **cannot** contain a secret (plaintext tokens are refused at load). By default nothing
needs one: GitHub goes through each dev's `gh` login, Jira through their `twg` login, Figma through the desktop app's
MCP server. Anything that does need a secret (a Slack webhook, Jira REST, an extra MCP server) references `${VAR}`,
stored once per machine with `factory secrets set VAR`; `factory install` renders MCP servers for Claude Code
(`${VAR}`) and Cursor (`${env:VAR}`) without writing values. Details: [docs/configuration.md](docs/configuration.md).

## Measuring it

- `factory metrics` — outcomes, PR rate, median time to PR, share of gates that ran automatically.
- `factory eval add APP-99 --fix-commit <sha>` → `factory eval prepare APP-99` → run the factory in the worktree →
  `factory eval score APP-99`. Replays tickets your team already fixed and compares against the human fix.
  Use it before raising anyone's autonomy ceiling. Details: [docs/evals.md](docs/evals.md).
- `factory events -f` — the JSONL event stream behind metrics, Slack gate notifications and the pixel office.

## The pixel office

Set `viz.pixel_agents: true` and each run shows up in [Pixel Agents](https://github.com/pixel-agents-hq/pixel-agents)
as a character: steps are tools it works on, gates are permission prompts it waits at. See [viz/pixel-agents](viz/pixel-agents/README.md).

## Commands

| | |
|---|---|
| `setup`, `init`, `install`, `doctor`, `secrets set/list`, `exec --` | setup |
| `run <KEY>`, `next`, `submit <node> <file>`, `schema <node>`, `resume` | the agent loop |
| `gate`, `approve <gate>`, `reject <gate>`, `abort` | humans |
| `status`, `risk`, `events`, `metrics` | visibility |
| `snap before/after <label>`, `snap diff`, `android install/where/tap/wait/open/back/flow` | device |
| `guardrail learn/review/check` | code review outside a run |
| `eval add/prepare/score/report` | measurement |

## Documentation

[Onboarding](docs/onboarding.md) · [Architecture](docs/architecture.md) · [Autonomy](docs/autonomy.md) · [Configuration](docs/configuration.md) ·
[Guardrail](docs/guardrail.md) · [Events & visualisation](docs/events.md) · [Evals](docs/evals.md) ·
[Adding a platform](docs/platforms.md) · [Roadmap](docs/roadmap.md)

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE)

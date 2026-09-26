# Pixel Agents bridge

[Pixel Agents](https://github.com/pixel-agents-hq/pixel-agents) (MIT) renders AI agents as pixel-art characters in an
office. Mobile Factory can put each run in that office.

## Enable

1. Run Pixel Agents (VS Code extension, or `npx pixel-agents` standalone).
2. In `factory.yaml`: `viz: {pixel_agents: true}`.

## How it works

Pixel Agents servers register themselves under `~/.pixel-agents/servers/*.json` (`port`, `token`, `pid`) and accept
hook events at `POST http://127.0.0.1:<port>/api/hooks/<provider>` with a bearer token. Its only shipped provider is
Claude Code, so the factory speaks that provider's payload shape (**compatibility mode**):

| Factory event | Hook payload | In the office |
|---|---|---|
| `run.started` | `SessionStart` (`session_id = factory-<run>`, `cwd`) | a character arrives |
| `node.started` | `PreToolUse`, `tool_name = factory:<node>` | works on the step |
| `node.completed` / `gate.decided` | `PostToolUse` | step done |
| `gate.waiting` | `Notification` `permission_prompt` | waits for you (permission bubble) |
| `agent.waiting` | `Stop` | idle until the agent submits |
| `run.finished` | `Stop` + `SessionEnd` | leaves |

Delivery is best-effort (1 s timeout, dead servers skipped) and never affects a run.

## Limits and next step

Compatibility mode depends on Pixel Agents' Claude hook contract (protocol version 1). The robust path is a native
`mobile-factory` `HookProvider` upstream, normalising these events directly (tracked in [roadmap](../../docs/roadmap.md)).

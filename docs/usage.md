# Using the factory

A hands-on guide: install, set up a repo, run tickets, answer gates, and read the results. Every command also takes
`-h` / `--help` (for example `factory run -h`, `factory android snap -h`).

## 1. Install

```bash
pipx install mobile-factory        # or: uv tool install mobile-factory
factory version
```

Needs Python 3.11+, `git`, `gh`, and for Android `adb`, a JDK and the project's `./gradlew`. Maestro is optional.

## 2. Set up a machine and a repo

Run these from the app's repo root:

```bash
factory init                  # first time: project questions -> factory.yaml, then Jira/GitHub/Slack/Figma access, then tools
factory setup -y              # install/log in to git, gh, twg, JDK, Android tools, Figma (add --optional for Maestro)
factory install --target all  # skills, subagents and MCP servers for Claude Code and Cursor (home folder only)
factory doctor                # verify tools, secrets, logins, device, config
```

| Command | Useful flags |
|---|---|
| `init` | `--reconfigure` rewrite `factory.yaml` · `--defaults` non-interactive · `--skip-setup` |
| `setup` | `-y` no per-tool prompts · `--optional` also Maestro |
| `install` | `--target claude\|cursor\|all` |
| `doctor` | `--offline` skip network · `--device` time every device primitive on the emulator |
| `uninstall` | `--dry-run` · `--keep-home` · `--global` also remove skills/subagents · `-y` · `--force` |

A repo already set up by the lead only needs `factory setup` and `factory doctor` on a new machine.

### Secrets

`factory.yaml` never holds a secret. Store them per machine:

```bash
factory secrets set SLACK_WEBHOOK_URL   # typed, never echoed
factory secrets list                    # names only, plus which ones factory.yaml needs
factory exec -- claude                  # run a command with the secrets in its environment
```

## 3. Workflows

The ticket's type picks the workflow (`tracker.pipelines`); force one with `--workflow`.

| Workflow | Ticket types | Result | Steps |
|---|---|---|---|
| `light` | Bug, Task, Story, Improvement (Sub-task: parent's) | draft PR | preflight → intake → branch → **work** → checks → review (detectors) → commit → pr_preview → publish |
| `spike` | Spike | report posted on the ticket | research → report preview → post |
| `epic` | Epic | stories created in Jira | split → tickets gate → create |
| `new-app` | App | spec, architecture, first slice PR, backlog | a human at every gate |

Custom workflows and overrides: [workflows.md](workflows.md).

## 4. Run a ticket

Easiest: ask the agent *"fix MMI-123 with the factory"*. It drives the loop below itself. By hand:

```bash
factory run MMI-123                         # start; advances to the first agent step or gate
factory run MMI-123 --base develop          # base branch (required when base_branch is 'ask')
factory run MMI-123 --autonomy 2 --tests    # cap autonomy; work step also adds unit tests
factory run MMI-123 --direction "Only touch the chat screen"
factory run MMI-123 --direction-file notes.md
factory run MMI-123 --workflow spike        # force a workflow
```

### The agent loop

```bash
factory next                        # what to do now: TASK / SKILL / SUBMIT, or the gate waiting on a human
factory submit work out/work.json   # submit it; an invalid file lists the missing fields
```

Repeat `next` → do the step → `submit` until the run publishes the draft PR.

### Steering a run

```bash
factory direct "Use the existing ChatRepository, don't add a new one"   # every later step sees it
factory resume                     # continue after a restart, fixed environment, or approval
factory abort --reason "wrong ticket" [--rollback]                   # stop; --rollback restores changed files
```

## 5. Gates (humans only)

When `factory next` prints **WAITING ON A HUMAN**, run in your own terminal:

```bash
factory next                 # the summary you are approving
factory approve pr           # type the shown code; refused without a TTY, so an agent cannot self-approve
factory reject pr --reason "PR body misses the tested flavours"
```

Gate names: `plan`, `repro`, `diff`, `review`, `pr`, `report`, `tickets`, `architecture`. Which ones need a human
depends on the autonomy level ([autonomy.md](autonomy.md)). Nothing is ever merged or marked ready.

## 6. Watch and measure

```bash
factory status                      # progress, risk and tokens of the active run
factory status --all                # every run
factory status --timeline [--summary | --step checks]   # where the time went
factory tokens [--all]              # model tokens per ticket
factory events -f                   # live JSONL event stream
factory metrics --since 2026-09-01 [--json]
```

Every run-scoped command accepts `--run <id>` to target a run other than the active one.

## 7. Device helpers (Android)

```bash
factory android install                    # build, install, launch the configured variant
factory android open chat                  # deeplink (full URI or path under android.deeplink_scheme)
factory android tap "Send"                 # tap by text / content-desc
factory android back
factory android snap before home           # screenshot + text state into <run>/snapshots/before/home
factory android replay flows/chat.yaml     # Maestro flow, pass/fail per step
factory android mock ...                   # override API responses via a local proxy
```

## 8. Code review outside a run

```bash
factory guardrail learn          # harvest code-owner comments from merged PRs
factory guardrail chunks         # parallel tally plan for the harvested reviews
factory guardrail review         # review context for the current branch
factory guardrail check findings.json
```

Details: [guardrail.md](guardrail.md).

## 9. Evals

```bash
factory eval add MMI-99 --fix-commit <sha>
factory eval prepare MMI-99      # worktree at the commit before the human fix
# run the factory in that worktree
factory eval score MMI-99
factory eval report
```

Details: [evals.md](evals.md).

## 10. Visualiser

```bash
factory viz status | start | stop | demo
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `factory run` refuses to start | `factory doctor`; commit or stash changes; check `gh auth status` |
| A step fails repeatedly | `factory status --timeline --step <step>`; add `factory direct "..."`, then `factory resume` |
| `approve` says no TTY | run it yourself in a real terminal, not through the agent |
| Wrong workflow picked | `factory abort --reason ...` then `factory run KEY --workflow <name>` |
| Start over cleanly | `factory abort --reason ... --rollback` |

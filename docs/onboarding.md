# Onboarding

Two jobs, done by different people:

| | Who | How often | Result |
|---|---|---|---|
| **Repo setup** | tech lead | once per repo | committed `factory.yaml`, `.factory/`, agent config |
| **Machine setup** | every dev / PM | once per laptop | tools installed, logged in; no tokens to paste |

## Repo setup (tech lead)

```bash
cd your-app
factory init                  # sets up your machine first, then detects modules, applicationId, launcher, base branch, twg site
```

Set `setup.tools.twg.install` in `factory.yaml` to your company's twg install command so every dev's
`factory setup` can install it.

1. Review `factory.yaml`: `base_branch`, `android.variant`, `tracker.projects`, `forbidden_paths`, `local_only_paths`.
2. `factory guardrail learn --bases <main branch>`, then have your agent distill `.factory/taste.md` with the
   `guardrail-learn` skill; the code owner approves it in a PR.
3. Write the first `.factory/knowledge/*.md` rules (traps, architecture boundaries).
4. `factory install --target all` and commit: `factory.yaml`, `.factory/{taste.md,knowledge/,flows/}`,
   `.claude/skills/`, `.cursor/rules/`, `.mcp.json`, `.cursor/mcp.json`, `CLAUDE.md`, `AGENTS.md`, `.gitignore`.
   None of these contain a secret; `factory.yaml` refuses to load if one does.

## Machine setup (each dev)

With the defaults, **no token is needed**: Jira goes through the dev's own `twg` login, GitHub through their own `gh`
login, Figma through the desktop app.

```bash
brew install pipx && pipx ensurepath                               # Homebrew: https://brew.sh
pipx install git+https://github.com/khawar-folio3/ai-factory-mobile
git clone <project repo> && cd <project>
factory setup                                                       # installs + logs in, asks before each step
factory doctor
```

`factory setup` checks each tool and offers the fix, one confirmation at a time:

| Tool | Installed with | Then |
|---|---|---|
| git, gh | `brew install` | `gh auth login --web` (browser) |
| twg | the command in `setup.tools.twg.install` (set once by the lead) | `twg login` (browser) |
| JDK, Android Studio, adb | `brew install --cask temurin@17 / android-studio / android-platform-tools` | open Android Studio once, create an emulator |
| Figma | `brew install --cask figma` | Figma → Preferences → **Enable Dev Mode MCP Server** (checked on the next run) |
| Maestro (optional) | official installer, with `factory setup --optional` | |

Re-run it any time; finished steps show `ok`. Claude Code or Cursor you install yourself.

`factory doctor` shows **who** will act for you — `gh authenticated (as <login>)`, `tracker reachable (twg as <name>
<email>)` — so a wrong account is caught before the first run. `warn` lines are optional.

Then open your agent in the repo and ask: *"fix APP-123 with the factory"*.

## Optional secrets

Only needed if the repo's `factory.yaml` references them; `factory secrets list` shows which are missing.

| Secret | When | Where to get it |
|---|---|---|
| `SLACK_WEBHOOK_URL` | gate / finish pings in a channel | ask the lead (one team webhook) |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | repo uses `tracker.provider: rest` (no `twg`) | id.atlassian.com → Security → API tokens |
| anything `${NAME}` under `mcp_servers` | an extra MCP server needs auth | that service |

Store each with `factory secrets set NAME` (hidden prompt, saved to `~/.config/mobile-factory/secrets.env`, mode
0600), and start the agent with `factory exec -- claude` or `factory exec -- cursor .` so it sees them.

## PMs

Same machine setup without Android Studio if you only file work: write tickets as Markdown in
`.factory/tickets/<KEY>.md` (with `tracker.kind: file`) or in Jira as usual, and let a dev's factory run pick them up.

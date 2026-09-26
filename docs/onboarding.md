# Onboarding

Two jobs, done by different people:

| | Who | How often | Result |
|---|---|---|---|
| **Repo setup** | tech lead | once per repo | committed `factory.yaml`, `.factory/`, agent config |
| **Machine setup** | every dev / PM | once per laptop | tools installed, logged in; no tokens to paste |

## One command: `factory init`

`factory init` is a guided setup that asks one thing at a time and checks every answer on the spot. The same command
serves the first person on a repo and everyone after them:

| Step | Asked | Checked with | Saved to |
|---|---|---|---|
| Project (first time only, or `--reconfigure`) | base branch (picked from remote branches), Gradle variant, Jira site URL, project keys, autonomy ceiling | — | `factory.yaml` (committed) |
| Jira sign-in | twg or API token. twg missing → paste your team's install command, it runs it; not logged in → runs `twg login` | `twg --site <site> whoami` / Jira `/myself` | `.factory/local.yaml`; tokens to the secrets file |
| GitHub | which logged-in `gh` account opens PRs here (the one that can push is preselected); none → `gh auth login --web` | repo push permission | `.factory/local.yaml` |
| Slack (optional) | incoming webhook URL | format | secrets file |
| Figma | remote server (default): nothing, you sign in on first use; desktop server: the Dev Mode toggle | desktop: local port 3845 | — |
| Machine tools | install git, gh, JDK, Android Studio, adb (and the Figma app if the repo uses the desktop server) | presence | — |

Nothing is hard-coded: each repo names its own Jira site, and each dev's answers stay on their machine. One
machine can work on several repos with different Jira sites and GitHub accounts at the same time: the factory uses
`twg --site <site>` and that repo's GitHub account for every call, without switching your active `gh` account.

Secrets get per-site / per-project names (`JIRA_API_TOKEN__ACME`, `SLACK_WEBHOOK_URL__MY_APP`) so projects never
overwrite each other's tokens. Values live only in `~/.config/mobile-factory/secrets.env` (0600); `factory.yaml` and
`.factory/local.yaml` hold references.

## Tech lead, first time on a repo

```bash
cd your-app && factory init
factory guardrail learn --bases <main branch>     # then distill .factory/taste.md with the guardrail-learn skill
factory install --target all
```

Commit `factory.yaml`, `.factory/{taste.md,knowledge/,flows/}`, `.claude/skills/`, `.cursor/rules/`, `.mcp.json`,
`.cursor/mcp.json`, `CLAUDE.md`, `AGENTS.md`, `.gitignore`. None of them contain a secret or anyone's account.

## Every dev, new machine

```bash
brew install pipx && pipx ensurepath                                 # Homebrew: https://brew.sh
pipx install git+https://github.com/khawar-folio3/ai-factory-mobile
git clone <project repo> && cd <project>
factory init                                                          # sees factory.yaml, asks only your part
factory doctor
```

`factory doctor` shows who acts for you (`gh authenticated (as <login>)`, `tracker reachable (twg as <name> <email>)`).
Re-run `factory init` any time to change your answers; `factory setup` re-checks only the machine tools.

## Figma

Repos default to Figma's remote MCP server (`https://mcp.figma.com/mcp`): no app or token; each dev's agent opens a
Figma sign-in the first time it reads a design. Switch a repo to the desktop server (`http://127.0.0.1:3845/mcp`) only
if your organisation requires it (Figma desktop → Design file → Dev Mode → inspect panel → MCP server → Enable desktop
MCP server). Either way, useful volume needs a **Dev or Full seat on a paid plan** (Professional: 200 calls/day);
Starter allows 20 calls per month.

## Removing the factory from a repo

```bash
factory uninstall --dry-run     # list what would go
factory uninstall               # asks once, then removes it
```

Removes `factory.yaml`, `.factory/` (runs, local settings), the installed skills and Cursor rules, the factory's MCP
entries, the Mobile Factory block in `CLAUDE.md` / `AGENTS.md`, and its `.gitignore` / `.git/info/exclude` lines.
Your own content in those files stays. It refuses while a run is open (`--force` to override). The `factory` command
and your secrets file are machine-wide and stay.

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

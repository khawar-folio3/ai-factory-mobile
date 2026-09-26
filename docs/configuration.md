# Configuration

Two files, and only two:

| File | Where | Committed | Holds |
|---|---|---|---|
| `factory.yaml` | repo root | yes | project, platform, autonomy, gates, tracker, guardrail, MCP servers — **no secrets** |
| secrets file | `~/.config/mobile-factory/secrets.env` (override: `secrets_file` or `$FACTORY_SECRETS_FILE`) | never | `NAME=value` lines, mode 0600 |

## Secrets

Reference secrets as `${NAME}` or `${NAME:-default}`. Resolution: process environment, then the secrets file.
Loading `factory.yaml` fails if any secret-looking key (`token`, `secret`, `password`, `api_key`, `authorization`,
`webhook`) holds a literal, or if any value looks like a known token format (GitHub, Slack, Atlassian, Figma, AWS…).

```bash
factory secrets set SLACK_WEBHOOK_URL  # prompts without echo, writes 0600
factory secrets list                   # names only: set (file) / set (env) / MISSING
factory exec -- claude                 # run any tool with the secrets in its environment
```

## MCP servers

GitHub and Jira need no MCP server: the factory and the agent use the `gh` and `twg` CLIs with each dev's own login.

```yaml
mcp_servers:
  figma:     {url: http://127.0.0.1:3845/mcp}           # Figma desktop app's local server, no token
  internal:
    command: npx
    args: [-y, "@acme/internal-mcp"]
    env: {API_KEY: "${ACME_KEY}"}
```

`factory install --target claude` merges them into `.mcp.json` (Claude Code expands `${VAR}`), `--target cursor`
into `.cursor/mcp.json` as `${env:VAR}`. Values are never written; start the tool through `factory exec` (or export
the variables yourself). Servers already in those files are kept.

## Reference

See the commented template written by `factory init`
([src/mobile_factory/templates/factory.yaml](../src/mobile_factory/templates/factory.yaml)) for every key and default.
Unknown keys are rejected so typos fail fast.

## Files under `.factory/`

| Path | Committed | |
|---|---|---|
| `taste.md` | yes | owner review rules (`factory guardrail learn` + the guardrail-learn skill) |
| `knowledge/*.md` | yes | tribal knowledge rules |
| `slop.yaml` | yes | detector overrides (`- id: S002\n  enabled: false`) |
| `flows/*.yaml` | yes | Maestro flows for reproduction/verification |
| `tickets/*.md` | optional | tickets for `tracker.kind: file` |
| `evals/cases/*.yaml` | yes | golden cases |
| `runs/`, `data/`, `events.jsonl`, `evals/work/` | no | run state, harvested reviews (contain names), logs |

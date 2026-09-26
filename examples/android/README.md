# Example: Android app with Jira

Copy these into your app repo and adjust. `factory init` generates the same `factory.yaml` with values detected from
your project; this example shows a filled-in Jira + MCP setup.

- `factory.yaml` — Jira over REST, Slack gate notifications, Figma/Atlassian/GitHub MCP servers
- `.factory/tickets/DEMO-1.md` — a ticket for `tracker.kind: file`, the no-Jira path (handy for PMs)
- `.factory/flows/open-profile.yaml` — a Maestro flow the reproduce/verify steps can reuse
- `.factory/taste.md` — what a distilled owner-taste file looks like

# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

### Added
- Bugfix pipeline (13 nodes) with resumable run state and bounded retries with rollback to a checkpoint.
- Risk-scored autonomy (levels 0–4) and five gates; TTY-only human approval with a one-time code.
- Guardrail: hard limits, 12 AI-slop detectors, owner-taste harvesting from merged PRs, tribal-knowledge rules.
- Android platform: gradle checks for touched modules, install, uiautomator screen state, snapshots and diff, tap/wait/deeplink, Maestro flows.
- Trackers: Atlassian `twg` CLI (default, no token), Jira REST, Markdown ticket files.
- No tokens required by default: GitHub via `gh`, Jira via `twg`, Figma via the desktop MCP server; `factory doctor` shows which accounts act for you.
- `factory setup` (run by `factory init`): installs and logs in to git, gh, twg, JDK, Android Studio, adb and Figma, one confirmation per step; twg installer configurable per repo.
- Single `factory.yaml` with `${VAR}` secrets, per-machine secrets file, plaintext-secret refusal, `factory exec`.
- Claude Code and Cursor adapters (skills/rules, MCP config, agent instructions block).
- Event stream with Slack and Pixel Agents sinks; metrics; eval harness replaying past tickets.

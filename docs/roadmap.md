# Roadmap

## v0.1 (this release)
- Bugfix pipeline, Android end to end
- Risk-scored autonomy, five gates, TTY-only approval
- Guardrail: limits, AI-slop detectors, taste harvesting, tribal knowledge
- Single config + per-machine secrets; Claude Code and Cursor adapters
- Event stream, Slack, Pixel Agents bridge, metrics, eval harness

## v0.2
- **iOS platform** (simulator build/install, hierarchy, snapshots, deeplinks)
- **Parity pipeline**: run one Maestro flow on both apps, diff screen states, pick the reference (Figma via MCP or a
  gate), write a behaviour spec, fix the other platform, re-verify both
- Navigation graph extractors (route tables → reachable screens) to choose adjacent screens automatically

## v0.3
- **POC pipeline for PMs**: brief → scaffold → preview build → Firebase App Distribution / TestFlight link
- Slack interactive approvals (a small Slack app; MCP cannot receive button callbacks)
- Guardrail feedback loop: record human reviewer agreement per rule after merge; demote noisy rules automatically
- Token/cost budget per run

## Later
- MCP server mode (`factory mcp`) exposing next/submit/status as tools
- Native Pixel Agents provider (instead of the Claude-hook compatibility bridge)
- Web dashboard over `events.jsonl`

from __future__ import annotations

import json
from pathlib import Path

from mobile_factory import adapters, config, events

YAML = """version: 1
project: {name: d}
mcp_servers:
  figma: {url: "http://127.0.0.1:3845/mcp"}
  atlassian: {url: "https://mcp.atlassian.com/v1/sse"}
  gh: {url: "https://api.githubcopilot.com/mcp/", headers: {Authorization: "Bearer ${GITHUB_TOKEN}"}}
  local: {command: npx, args: [-y, some-mcp], env: {API_KEY: "${SOME_KEY}"}}
"""


def lc(tmp_path: Path) -> config.LoadedConfig:
    (tmp_path / "factory.yaml").write_text(YAML)
    return config.load(tmp_path)


def test_mcp_json_keeps_references(tmp_path: Path) -> None:
    c = adapters.mcp_json(lc(tmp_path), "claude")["mcpServers"]
    assert c["gh"]["headers"]["Authorization"] == "Bearer ${GITHUB_TOKEN}"
    assert c["atlassian"]["type"] == "sse" and c["figma"]["type"] == "http"
    assert c["local"] == {"command": "npx", "args": ["-y", "some-mcp"], "env": {"API_KEY": "${SOME_KEY}"}}
    cur = adapters.mcp_json(lc(tmp_path), "cursor")["mcpServers"]
    assert cur["gh"]["headers"]["Authorization"] == "Bearer ${env:GITHUB_TOKEN}"
    assert "type" not in cur["figma"]


def test_install_claude_and_cursor(tmp_path: Path) -> None:
    c = lc(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"url": "http://keep"}}}))
    (tmp_path / "CLAUDE.md").write_text("# House rules\n")
    adapters.install(c, "claude")
    adapters.install(c, "claude")  # idempotent
    skill = (tmp_path / ".claude/skills/factory/SKILL.md").read_text()
    assert skill.startswith("---\nname: factory\n")
    assert (tmp_path / ".claude/skills/factory-review/SKILL.md").read_text().startswith("---\nname: factory-review\n")
    servers = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert {"mine", "figma", "gh"} <= servers.keys()
    md = (tmp_path / "CLAUDE.md").read_text()
    assert md.startswith("# House rules") and md.count(adapters.BLOCK_START) == 1

    adapters.install(c, "cursor")
    rule = (tmp_path / ".cursor/rules/factory-fix.mdc").read_text()
    assert rule.startswith("---\ndescription:") and rule.count("---") == 2
    assert (tmp_path / "AGENTS.md").read_text().count(adapters.BLOCK_END) == 1


def test_event_log_and_pixel_payloads(tmp_path: Path) -> None:
    got: list[events.Event] = []
    bus = events.EventBus(tmp_path / "events.jsonl", [got.append, lambda e: 1 / 0])
    bus.emit(events.NODE_STARTED, "r1", node="fix", title="Fix")
    assert events.read(tmp_path / "events.jsonl", "r1")[0]["node"] == "fix"
    assert len(got) == 1  # the failing sink did not break the bus

    px = events.PixelAgentsSink(tmp_path)
    start = px.payloads({"type": events.NODE_STARTED, "run": "r1", "node": "fix", "title": "Fix"})[0]
    assert start["hook_event_name"] == "PreToolUse" and start["tool_name"] == "factory:fix"
    assert start["session_id"] == "factory-r1"
    gate = px.payloads({"type": events.GATE_WAITING, "run": "r1", "gate": "pr"})[0]
    assert gate["notification_type"] == "permission_prompt"
    end = px.payloads({"type": events.RUN_FINISHED, "run": "r1", "outcome": "draft-pr"})
    assert [p["hook_event_name"] for p in end] == ["Stop", "SessionEnd"]


def test_pixel_sink_without_servers_is_silent(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(events.PixelAgentsSink, "home", tmp_path / "none")
    events.PixelAgentsSink(tmp_path)({"type": events.RUN_STARTED, "run": "r1"})

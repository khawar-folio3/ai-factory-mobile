from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _repo_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_install_writes_only_to_the_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = lc(tmp_path)
    home = adapters.agent_home()
    before = _repo_files(tmp_path)
    registered: list[list[str]] = []
    monkeypatch.setattr(adapters, "has", lambda tool: True)
    monkeypatch.setattr(adapters, "_register_claude_mcp", lambda root, servers: registered.append(sorted(servers)))
    adapters.install(c, "claude")
    adapters.install(c, "claude")  # idempotent
    adapters.install(c, "cursor")
    assert _repo_files(tmp_path) == before  # the repo is never touched
    assert (home / ".claude/skills/factory/SKILL.md").read_text().startswith("---\nname: factory\n")
    assert (home / ".cursor/skills/factory-review/SKILL.md").read_text().startswith("---\nname: factory-review\n")
    assert registered[0] == ["atlassian", "figma", "gh", "local"]  # Claude: local scope, kept in ~/.claude.json
    assert {"figma", "gh"} <= json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"].keys()
    allowed = json.loads((home / ".claude/settings.json").read_text())["permissions"]["additionalDirectories"]
    assert allowed == [str(config.state_home())]  # once, however often install runs


def test_claude_subagents_carry_per_step_models(tmp_path: Path) -> None:
    (tmp_path / "factory.yaml").write_text("version: 1\nproject: {name: d}\nagents: {models: {fix: sonnet}}\n")
    adapters.install(config.load(tmp_path), "claude")
    agents = adapters.agent_home() / ".claude/agents"
    fix = (agents / "factory-fix.md").read_text()
    assert fix.startswith("---\nname: factory-fix\n") and "\nmodel: sonnet\n" in fix  # override
    assert "\nmodel: haiku\n" in (agents / "factory-review-detectors.md").read_text()  # default kept
    assert "Read-only" in (agents / "factory-review-taste.md").read_text()
    assert "Read-only" not in fix and "Never run `factory submit`" in fix
    assert not (agents / "factory-factory.md").exists()  # the driver stays a skill


def test_cursor_subagents_map_tiers_to_cursor_ids(tmp_path: Path) -> None:
    (tmp_path / "factory.yaml").write_text(
        "version: 1\nproject: {name: d}\nagents: {cursor_models: {opus: my-opus-id}}\n"
    )
    adapters.install(config.load(tmp_path), "cursor")
    agents = adapters.agent_home() / ".cursor/agents"
    assert "\nmodel: my-opus-id\n" in (agents / "factory-fix.md").read_text()
    assert "\nmodel: inherit\n" in (agents / "factory-triage.md").read_text()  # sonnet not mapped yet
    assert "Read-only" in (agents / "factory-locate.md").read_text()  # told, not flagged: it must write its file
    assert "readonly" not in (agents / "factory-review-taste.md").read_text()


def test_event_log_survives_a_failing_sink(tmp_path: Path) -> None:
    got: list[events.Event] = []
    bus = events.EventBus(tmp_path / "events.jsonl", [got.append, lambda e: 1 / 0])
    bus.emit(events.NODE_STARTED, "r1", node="fix", title="Fix")
    assert events.read(tmp_path / "events.jsonl", "r1")[0]["node"] == "fix"
    assert len(got) == 1  # the failing sink did not break the bus

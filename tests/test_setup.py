from __future__ import annotations

from pathlib import Path

import pytest

from mobile_factory import setup as machine
from mobile_factory.config import SetupConfig, ToolOverride


def tool(name: str, present: bool, install: str = "brew install x", **kw: object) -> machine.Tool:
    return machine.Tool(name, "why", lambda: present, install, **kw)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def brew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "has", lambda t: t == "brew")


def test_plan_installs_missing_then_logs_in() -> None:
    p = machine.plan(
        [
            tool("git", True),
            tool("gh", False, "brew install gh", login="gh auth login --web", logged_in=lambda: False),
            tool("twg", True, "", login="twg login", logged_in=lambda: False),
        ]
    )
    assert [(s.tool, s.action) for s in p.steps] == [
        ("git", "ok"),
        ("gh", "install"),
        ("gh", "login"),
        ("twg", "login"),
    ]


def test_tool_without_installer_is_missing_and_not_logged_into() -> None:
    p = machine.plan([tool("twg", False, "", login="twg login", after="ask your lead")])
    ran: list[str] = []
    assert not machine.execute(p, lambda s: True, runner=lambda c: ran.append(c) or 0, echo=lambda m: None)
    assert p.steps[0].action == "missing" and "ask your lead" in p.steps[0].note
    assert ran == []


def test_figma_needs_manual_mcp_toggle_after_install() -> None:
    p = machine.plan([tool("figma", False, "brew install --cask figma", ready=lambda: False, after="enable MCP")])
    assert [s.action for s in p.steps] == ["install", "manual"]
    p = machine.plan([tool("figma", True, ready=lambda: True)])
    assert [s.action for s in p.steps] == ["ok"]


def test_no_homebrew_means_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "has", lambda t: False)
    p = machine.plan([tool("gh", False, "brew install gh")])
    assert p.steps[0].action == "missing" and "brew.sh" in p.steps[0].note


def test_optional_tools_only_on_request() -> None:
    t = tool("maestro", False, "curl x | bash", optional=True)
    assert machine.plan([t]).steps == []
    assert machine.plan([t], optional=True).steps[0].action == "install"


def test_execute_confirms_and_stops_tool_after_failure() -> None:
    p = machine.plan([tool("gh", False, "brew install gh", login="gh auth login", logged_in=lambda: False)])
    ran: list[str] = []
    assert not machine.execute(p, lambda s: True, runner=lambda c: ran.append(c) or 1, echo=lambda m: None)
    assert ran == ["brew install gh"]  # no login attempt after the install failed
    ran.clear()
    assert not machine.execute(p, lambda s: False, runner=lambda c: ran.append(c) or 0, echo=lambda m: None)
    assert ran == []


def test_lead_provides_twg_installer_and_can_skip_tools() -> None:
    cfg = SetupConfig(
        tools={
            "twg": ToolOverride(install="curl -fsSL https://example.com/twg.sh | sh"),
            "figma": ToolOverride(skip=True),
        }
    )
    by_name = {t.name: t for t in machine.tools(cfg)}
    assert by_name["twg"].install.startswith("curl") and by_name["twg"].after == ""
    assert "figma" not in by_name
    assert {"git", "gh", "twg", "java", "android-studio", "adb", "maestro"} <= by_name.keys()


def test_figma_mode_follows_the_configured_server() -> None:
    from mobile_factory.config import FactoryConfig, figma_mode

    def cfg(url: str) -> FactoryConfig:
        return FactoryConfig.model_validate(
            {"project": {"name": "d"}, "mcp_servers": {"figma": {"url": url}} if url else {}}
        )

    assert figma_mode(cfg("http://127.0.0.1:3845/mcp")) == "desktop"
    assert figma_mode(cfg("https://mcp.figma.com/mcp")) == "remote"
    assert figma_mode(cfg("")) == "none"
    assert "figma" in {t.name for t in machine.tools(None, "desktop")}
    assert "figma" not in {t.name for t in machine.tools(None, "remote")}


def test_installs_go_to_installer_and_logins_to_the_terminal() -> None:
    p = machine.Plan(
        [machine.Step("maestro", "install", "curl maestro"), machine.Step("gh", "login", "gh auth login --web")]
    )
    installed: list[str] = []
    ran: list[str] = []
    ok = machine.execute(
        p,
        lambda s: True,
        runner=lambda c: ran.append(c) or 0,
        echo=lambda m: None,
        installer=lambda s: installed.append(s.command) or 0,
    )
    assert ok and installed == ["curl maestro"] and ran == ["gh auth login --web"]


def test_only_the_chosen_agent_cli_is_set_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "LOCAL_BIN", tmp_path)  # no stray ~/.local/bin/claude from this machine
    names = lambda agents: {t.name for t in machine.tools(agents=agents)}  # noqa: E731
    assert "claude" in names(["claude"]) and "cursor-cli" not in names(["claude"])
    assert {"claude", "cursor-cli"} <= names(["claude", "cursor"])
    claude = next(t for t in machine.tools(agents=["claude"]) if t.name == "claude")
    assert claude.install == "curl -fsSL https://claude.ai/install.sh | bash" and claude.login == "claude auth login"


def test_which_finds_fresh_installs_in_local_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mobile_factory import proc

    tool = tmp_path / "freshcli"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    monkeypatch.setattr(proc, "LOCAL_BIN", tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert proc.which("freshcli") == str(tool) and proc.run(["freshcli"]).out.strip() == "hi"


def test_broken_claude_link_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "claude").symlink_to(tmp_path / "gone/2.1.246/claude")
    monkeypatch.setattr(machine, "LOCAL_BIN", tmp_path)
    assert "points to a removed build" in machine._claude_why()
    claude = next(t for t in machine.tools(agents=["claude"]) if t.name == "claude")
    assert claude.install == f"rm -f '{tmp_path / 'claude'}' && {machine.CLAUDE_INSTALL}"


def test_optional_extras_never_block_machine_ready() -> None:
    p = machine.Plan([machine.Step("claude-office", "install", "git clone …", soft=True)])
    assert machine.execute(p, lambda s: True, echo=lambda m: None, installer=lambda s: 1)
    p = machine.Plan([machine.Step("claude", "install", "curl claude")])
    assert not machine.execute(p, lambda s: True, echo=lambda m: None, installer=lambda s: 1)


def test_outdated_reads_brew_and_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from mobile_factory.proc import Result

    brew = {
        "formulae": [{"name": "gh", "installed_versions": ["2.60.0"], "current_version": "2.62.0"}],
        "casks": [{"name": "android-platform-tools", "installed_versions": ["35.0.1"], "current_version": "36.0.0"}],
    }
    monkeypatch.setattr(machine, "has", lambda tool: True)
    monkeypatch.setattr(
        machine,
        "run",
        lambda cmd, **_: Result(0 if cmd[0] == "brew" else 1, json.dumps(brew), ""),
    )
    tools = [machine.Tool(n, "", lambda: True) for n in ("gh", "adb", "git")]
    ups = {u.tool: u for u in machine.outdated(tools)}
    assert set(ups) == {"gh", "adb"}  # git is current
    assert ups["gh"].command == "brew upgrade gh" and ups["gh"].latest == "2.62.0"
    assert ups["adb"].command == "brew upgrade --cask android-platform-tools"

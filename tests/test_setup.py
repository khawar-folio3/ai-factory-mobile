from __future__ import annotations

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

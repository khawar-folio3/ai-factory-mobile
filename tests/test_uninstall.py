from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakePlatform, git
from typer.testing import CliRunner

from mobile_factory import adapters, config
from mobile_factory import uninstall as remover
from mobile_factory.cli import app
from mobile_factory.init import GITIGNORE

runner = CliRunner()


@pytest.fixture
def installed(repo: Path) -> Path:
    with (repo / "factory.yaml").open("a") as f:
        f.write("mcp_servers:\n  figma: {url: https://mcp.figma.com/mcp}\n")
    (repo / "CLAUDE.md").write_text("# House rules\nKeep it small.\n")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"url": "http://keep"}}}))
    (repo / ".gitignore").write_text("build/\n\n# mobile-factory\n" + "\n".join(GITIGNORE) + "\n")
    (repo / ".factory/local.yaml").write_text("limits: {max_fix_attempts: 1}\n")
    exclude = repo / ".git/info/exclude"
    exclude.write_text("# mine\n*.swp\n/factory.yaml\n/.factory/\n")
    lc = config.load(repo)
    adapters.install(lc, "claude")
    adapters.install(lc, "cursor")
    return repo


def test_uninstall_removes_only_factory_things(installed: Path) -> None:
    root = installed
    rm = remover.plan(root)
    rendered = rm.render(root)
    assert "delete  factory.yaml" in rendered and "delete  .factory/" in rendered
    assert ".mcp.json: remove MCP servers figma" in rendered
    assert "factory.yaml" in rm.tracked

    remover.apply(root, rm)

    assert not (root / "factory.yaml").exists() and not (root / ".factory").exists()
    assert not list((root / ".claude").glob("skills/factory*")) if (root / ".claude").exists() else True
    assert not (root / ".cursor").exists()  # only factory rules and mcp were there
    assert not (root / "AGENTS.md").exists()  # only the factory block was there
    assert (root / "CLAUDE.md").read_text() == "# House rules\nKeep it small.\n"
    assert json.loads((root / ".mcp.json").read_text()) == {"mcpServers": {"mine": {"url": "http://keep"}}}
    assert (root / ".gitignore").read_text() == "build/\n"
    assert (root / ".git/info/exclude").read_text() == "# mine\n*.swp\n"
    assert remover.plan(root).render(root).startswith("nothing to remove")


def test_cli_dry_run_and_open_run_guard(installed: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(installed)
    git(installed, "add", "-A")
    git(installed, "commit", "-q", "-m", "installed")
    assert runner.invoke(app, ["run", "APP-1"]).exit_code == 0

    r = runner.invoke(app, ["uninstall", "--dry-run"])
    assert r.exit_code == 0 and "delete  factory.yaml" in r.output
    assert (installed / "factory.yaml").exists()

    r = runner.invoke(app, ["uninstall", "--yes"])
    assert r.exit_code != 0 and (installed / "factory.yaml").exists()  # open run blocks it

    r = runner.invoke(app, ["uninstall", "--yes", "--force"])
    assert r.exit_code == 0, r.output
    assert not (installed / "factory.yaml").exists()


def test_uninstall_needs_confirmation_from_a_human(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(installed)
    r = runner.invoke(app, ["uninstall"])  # CliRunner is not a TTY
    assert r.exit_code != 0 and (installed / "factory.yaml").exists()


def test_gitignore_restored_byte_for_byte(repo: Path) -> None:
    (repo / ".gitignore").write_text("build/\n/.cursor")  # committed without a final newline
    git(repo, "commit", "-q", "-am", "gitignore")
    from mobile_factory.init import init as do_init

    do_init(repo, force=True)
    assert "# mobile-factory" in (repo / ".gitignore").read_text()
    remover.apply(repo, remover.plan(repo))
    assert (repo / ".gitignore").read_text() == "build/\n/.cursor"

from __future__ import annotations

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
def legacy(repo: Path) -> Path:
    """A repo set up by an older version: factory files and agent integration inside the repo."""
    (repo / "factory.yaml").write_text(config.config_path(repo).read_text())
    (repo / ".factory/knowledge").mkdir(parents=True)
    (repo / ".factory/knowledge/README.md").write_text("tribal\n")
    (repo / "CLAUDE.md").write_text(
        "# House rules\nKeep it small.\n\n" + adapters.BLOCK_START + "\nold\n" + adapters.BLOCK_END + "\n"
    )
    (repo / ".claude/skills/factory").mkdir(parents=True)
    (repo / ".claude/skills/factory/SKILL.md").write_text("old skill\n")
    (repo / ".gitignore").write_text("build/\n\n# mobile-factory\n" + "\n".join(GITIGNORE) + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "old factory layout")
    return repo


def test_install_moves_an_old_layout_out_of_the_repo(legacy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(legacy)
    r = runner.invoke(app, ["install", "--target", "claude"])
    assert r.exit_code == 0, r.output
    assert not (legacy / ".claude/skills/factory").exists() and not (legacy / "factory.yaml").exists()
    assert (legacy / "CLAUDE.md").read_text() == "# House rules\nKeep it small.\n"
    assert (legacy / ".gitignore").read_text() == "build/\n"
    assert (config.state_dir(legacy) / "knowledge/README.md").read_text() == "tribal\n"  # team files kept, moved
    assert (adapters.agent_home() / ".claude/skills/factory/SKILL.md").is_file()
    assert "repo untouched" in r.output


def test_uninstall_removes_the_factory_home_and_keeps_the_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = config.state_dir(repo)
    rm = remover.plan(repo)
    assert rm.delete == [home] and not rm.tracked
    assert "delete  ~" in rm.render(repo) or str(home) in rm.render(repo)
    before = git(repo, "status", "--porcelain")
    remover.apply(repo, rm)
    assert not home.exists() and git(repo, "status", "--porcelain") == before
    assert remover.plan(repo).render(repo).startswith("nothing to remove")


def test_keep_home_and_global_options(repo: Path) -> None:
    adapters.install(config.load(repo), "claude")
    assert remover.plan(repo, home=False).delete == []
    rm = remover.plan(repo, home=False, global_=True)
    assert any(p.name == "factory" for p in rm.delete) and any(p.name == "factory-fix.md" for p in rm.delete)


def test_cli_dry_run_and_open_run_guard(repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    assert runner.invoke(app, ["run", "APP-1"]).exit_code == 0
    home = config.state_dir(repo)

    r = runner.invoke(app, ["uninstall", "--dry-run"])
    assert r.exit_code == 0 and "delete" in r.output and home.exists()

    r = runner.invoke(app, ["uninstall", "--yes"])
    assert r.exit_code != 0 and home.exists()  # open run blocks it

    r = runner.invoke(app, ["uninstall", "--yes", "--force"])
    assert r.exit_code == 0, r.output
    assert not home.exists()


def test_uninstall_needs_confirmation_from_a_human(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    r = runner.invoke(app, ["uninstall"])  # CliRunner is not a TTY
    assert r.exit_code != 0 and config.config_path(repo).exists()


def test_old_gitignore_lines_removed_byte_for_byte(repo: Path) -> None:
    (repo / ".gitignore").write_text("build/\n/.cursor")  # committed without a final newline
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "gitignore")
    (repo / ".gitignore").write_text("build/\n/.cursor\n\n# mobile-factory\n" + "\n".join(GITIGNORE) + "\n")
    remover.apply(repo, remover.plan(repo))
    assert (repo / ".gitignore").read_text() == "build/\n/.cursor"

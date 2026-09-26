from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mobile_factory import adapters, config
from mobile_factory.platforms.base import Check, CheckRun, Platform
from mobile_factory.viz.pixel import PixelAgents


@pytest.fixture(autouse=True)
def factory_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Factory and agent homes are throwaway folders: tests never touch the real ~/.config, ~/.claude or ~/.cursor."""
    home = tmp_path_factory.mktemp("factory-home")
    monkeypatch.setenv("FACTORY_HOME", str(home))
    monkeypatch.setenv("FACTORY_AGENT_HOME", str(tmp_path_factory.mktemp("agent-home")))
    monkeypatch.setattr(adapters, "_register_claude_mcp", lambda root, servers: sorted(servers))  # no ~/.claude.json
    monkeypatch.setattr(PixelAgents, "home", tmp_path_factory.mktemp("pixel-home"))  # no ~/.pixel-agents
    return home


FACTORY_YAML = """
version: 1
project:
  name: demo
  base_branch: main
android:
  modules: [app, core]
  application_id: com.example.demo
autonomy:
  ceiling: {ceiling}
tracker:
  kind: file
limits:
  max_fix_attempts: 2
  max_review_rounds: 2
"""

TICKET = """---
type: Bug
---
# Avatar is clipped on the profile screen

Steps: open Profile. Expected: full avatar. Actual: top half cut off.
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakePlatform(Platform):
    name = "fake"

    def __init__(self) -> None:
        self.state = "activity  Main\nlabels    avatar-clipped\n"
        self.fail_checks = False
        self.check_calls = 0

    def doctor(self) -> list[Check]:
        return [Check("fake device", True)]

    def ensure_device(self) -> str:
        return "fake-1"

    def build_install(self, log_dir: Path, launch: bool = True) -> CheckRun:
        return CheckRun(True, "installed")

    def modules_for(self, files: list[str]) -> list[str]:
        return sorted({f.split("/")[0] for f in files if f.split("/")[0] in ("app", "core")})

    def checks(self, files: list[str], log_dir: Path) -> CheckRun:
        self.check_calls += 1
        return CheckRun(not self.fail_checks, "FAILED: :app:testDebugUnitTest" if self.fail_checks else "ok")

    def screen_state(self) -> str:
        return self.state

    def screenshot(self, dest: Path) -> None:
        dest.write_bytes(b"png")

    def tap(self, label: str, nth: int = 1) -> str:
        return f"tapped {label}"

    def wait_for(self, text: str, timeout: int = 30) -> bool:
        return True

    def open_link(self, link: str) -> str:
        return link

    def back(self) -> None:
        return None

    def launch(self) -> str:
        return "launched"

    def run_flow(self, flow: Path, log_dir: Path) -> CheckRun:
        return CheckRun(True, "ok")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "dev@example.com")
    git(work, "config", "user.name", "Dev")
    git(work, "config", "commit.gpgsign", "false")
    (work / "app/src/main/java").mkdir(parents=True)
    (work / "app/src/main/java/Profile.kt").write_text("class Profile {\n    val height = 48\n}\n")
    (work / "app/build.gradle.kts").write_text('plugins { id("com.android.application") }\n')
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "push", "-q", "origin", "main")
    config.config_path(work).parent.mkdir(parents=True, exist_ok=True)  # the factory lives outside the repo
    config.config_path(work).write_text(FACTORY_YAML.format(ceiling=4))
    (config.state_dir(work) / "tickets").mkdir()
    (config.state_dir(work) / "tickets/APP-1.md").write_text(TICKET)
    return work


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    p = FakePlatform()
    published: dict[str, Any] = {}
    monkeypatch.setattr("mobile_factory.pipeline.make_platform", lambda lc: p)
    monkeypatch.setattr("mobile_factory.pipeline.github.authenticated", lambda root: True)
    monkeypatch.setattr(
        "mobile_factory.pipeline.github.push", lambda root, remote, branch: published.update(branch=branch)
    )
    monkeypatch.setattr(
        "mobile_factory.pipeline.github.create_pr",
        lambda root, base, branch, title, body, **kw: (
            published.update(title=title) or "https://github.com/acme/demo/pull/7"
        ),
    )
    p.published = published  # type: ignore[attr-defined]
    return p


def load(repo: Path, ceiling: int | None = None) -> config.LoadedConfig:
    if ceiling is not None:
        config.config_path(repo).write_text(FACTORY_YAML.format(ceiling=ceiling))
    return config.load(repo)

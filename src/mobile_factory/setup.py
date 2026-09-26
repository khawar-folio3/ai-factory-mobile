from __future__ import annotations

import platform
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import SetupConfig
from .proc import has, run

FIGMA_MCP_PORT = 3845


def _app(name: str) -> Callable[[], bool]:
    return lambda: any((d / f"{name}.app").exists() for d in (Path("/Applications"), Path.home() / "Applications"))


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class Tool:
    name: str
    why: str
    present: Callable[[], bool]
    install: str = ""
    login: str = ""
    logged_in: Callable[[], bool] | None = None
    after: str = ""
    optional: bool = False
    ready: Callable[[], bool] | None = None


@dataclass
class Step:
    tool: str
    action: str  # install | login | manual | ok | missing
    command: str = ""
    note: str = ""


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    @property
    def todo(self) -> list[Step]:
        return [s for s in self.steps if s.action != "ok"]

    def render(self) -> str:
        mark = {"ok": "ok     ", "install": "install", "login": "login  ", "manual": "manual ", "missing": "MISSING"}
        return "\n".join(
            f"{mark[s.action]}  {s.tool:<16}"
            + (f"  $ {s.command}" if s.command else "")
            + (f"  ({s.note})" if s.note else "")
            for s in self.steps
        )


def tools(cfg: SetupConfig | None = None) -> list[Tool]:
    mac = platform.system() == "Darwin"

    def cask(name: str) -> str:
        return f"brew install --cask {name}" if mac else ""

    listed = [
        Tool("git", "version control", lambda: has("git"), "brew install git"),
        Tool(
            "gh",
            "branches, pushes, PRs, review history",
            lambda: has("gh"),
            "brew install gh",
            login="gh auth login --hostname github.com --git-protocol https --web",
            logged_in=lambda: run(["gh", "auth", "status"]).ok,
        ),
        Tool(
            "twg",
            "Jira tickets with your own Atlassian login",
            lambda: has("twg"),
            "",
            login="twg login",
            logged_in=lambda: run(["twg", "whoami"]).ok,
            after="run `factory init`: it asks for your team's twg install command",
        ),
        Tool(
            "java",
            "Gradle builds",
            lambda: has("java") or _app("Android Studio")(),
            cask("temurin@17"),
        ),
        Tool("android-studio", "Android SDK, emulator", _app("Android Studio"), cask("android-studio")),
        Tool(
            "adb",
            "device control",
            lambda: has("adb"),
            cask("android-platform-tools"),
            after="or add <Android SDK>/platform-tools to PATH",
        ),
        Tool(
            "figma",
            "designs for the agent via Figma's desktop MCP server",
            _app("Figma"),
            cask("figma"),
            ready=lambda: _port_open(FIGMA_MCP_PORT),
            after="open Figma → Preferences → Enable Dev Mode MCP Server (needs a Dev or Full seat)",
        ),
        Tool(
            "maestro",
            "multi-step UI flows",
            lambda: has("maestro"),
            "curl -fsSL https://get.maestro.mobile.dev | bash",
            optional=True,
        ),
    ]
    overrides = (cfg or SetupConfig()).tools
    out = []
    for t in listed:
        o = overrides.get(t.name)
        if o and o.skip:
            continue
        if o and o.install:
            t.install, t.after = o.install, ""
        out.append(t)
    return out


def plan(tool_list: list[Tool], optional: bool = False) -> Plan:
    p = Plan()
    brew_missing = not has("brew")
    for t in tool_list:
        if t.optional and not optional and not t.present():
            continue
        if not t.present():
            if not t.install:
                p.steps.append(Step(t.name, "missing", note=t.after or f"install {t.name} manually"))
            elif brew_missing and t.install.startswith("brew "):
                p.steps.append(Step(t.name, "missing", t.install, "install Homebrew first: https://brew.sh"))
            else:
                p.steps.append(Step(t.name, "install", t.install, t.why))
            if t.login:
                p.steps.append(Step(t.name, "login", t.login, "after install"))
            if t.ready and t.after:
                p.steps.append(Step(t.name, "manual", note=t.after))
            continue
        if t.login and t.logged_in and not t.logged_in():
            p.steps.append(Step(t.name, "login", t.login, "not logged in"))
        elif t.ready and not t.ready():
            p.steps.append(Step(t.name, "manual", note=t.after))
        else:
            p.steps.append(Step(t.name, "ok"))
    return p


Runner = Callable[[str], int]


def shell(command: str) -> int:
    """Run with the user's terminal attached: installers and browser logins are interactive."""
    return subprocess.call(command, shell=True)  # noqa: S602 - fixed commands from the tool table or the lead's config


def execute(
    p: Plan, confirm: Callable[[Step], bool], runner: Runner = shell, echo: Callable[[str], None] = print
) -> bool:
    ok = True
    failed: set[str] = set()
    for s in p.todo:
        if s.tool in failed:
            continue
        if s.action in ("manual", "missing"):
            echo(f"→ {s.tool}: {s.note}" + (f"\n  $ {s.command}" if s.command else ""))
            ok = False
            if s.action == "missing":
                failed.add(s.tool)
            continue
        if not confirm(s):
            echo(f"skipped {s.tool} {s.action}")
            ok = False
            continue
        echo(f"$ {s.command}")
        if runner(s.command) != 0:
            echo(f"✕ {s.tool} {s.action} failed")
            failed.add(s.tool)
            ok = False
    return ok

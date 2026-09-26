from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import SetupConfig
from .proc import LOCAL_BIN, has, run

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
    group: str = ""
    soft: bool = False  # opted-in extra: installed by setup, but missing or failing never blocks "machine ready"


@dataclass
class Step:
    tool: str
    action: str  # install | login | manual | ok | missing
    command: str = ""
    note: str = ""
    group: str = ""
    why: str = ""
    soft: bool = False


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


DESKTOP_HINT = "Figma desktop app → open a Design file → Dev Mode (Shift+D) → inspect panel → MCP server → Enable desktop MCP server (paid plan, Dev or Full seat)"


CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"


def _claude_link() -> Path | None:
    """A dangling ~/.local/bin/claude (e.g. to a Claude desktop build that was updated away) blocks the installer."""
    link = LOCAL_BIN / "claude"
    return link if link.is_symlink() and not link.exists() else None


def _claude_why() -> str:
    if link := _claude_link():
        return f"{link} points to a removed build ({link.readlink()}); the broken link is removed first"
    return "Claude Code: runs agent steps such as taste distill"


def _claude_install() -> str:
    link = _claude_link()
    return f"rm -f '{link}' && {CLAUDE_INSTALL}" if link else CLAUDE_INSTALL


def _node_20() -> bool:
    m = re.match(r"v(\d+)", run(["node", "-v"]).out.strip()) if has("node") else None
    return m is not None and int(m.group(1)) >= 20


def tools(
    cfg: SetupConfig | None = None,
    figma: str = "desktop",
    agents: Sequence[str] | None = None,
    pixel: bool = False,
) -> list[Tool]:
    mac = platform.system() == "Darwin"

    def cask(name: str) -> str:
        return f"brew install --cask {name}" if mac else ""

    listed = [
        Tool("git", "version control", lambda: has("git"), "brew install git", group="Source control"),
        Tool(
            "gh",
            "branches, pushes, PRs, review history",
            lambda: has("gh"),
            "brew install gh",
            login="gh auth login --hostname github.com --git-protocol https --web",
            logged_in=lambda: run(["gh", "auth", "status"]).ok,
            group="Source control",
        ),
        Tool(
            "twg",
            "Jira tickets with your own Atlassian login",
            lambda: has("twg"),
            "",
            login="twg login",
            logged_in=lambda: run(["twg", "whoami"]).ok,
            after="run `factory init`: it asks for your team's twg install command",
            group="Tickets",
        ),
        Tool(
            "claude",
            _claude_why(),
            lambda: has("claude"),
            _claude_install(),
            login="claude auth login",
            logged_in=lambda: run(["claude", "auth", "status"]).ok,
            group="Coding agent",
        ),
        Tool(
            "cursor-cli",
            "Cursor CLI (`agent`): runs agent steps such as taste distill",
            lambda: has("agent") or has("cursor-agent"),
            "curl https://cursor.com/install -fsS | bash",
            login="agent login",
            logged_in=lambda: run(["agent", "status"]).ok,
            group="Coding agent",
        ),
        Tool(
            "java",
            "Gradle builds",
            lambda: has("java") or _app("Android Studio")(),
            cask("temurin@17"),
            group="Android",
        ),
        Tool(
            "android-studio", "Android SDK, emulator", _app("Android Studio"), cask("android-studio"), group="Android"
        ),
        Tool(
            "adb",
            "device control",
            lambda: has("adb"),
            cask("android-platform-tools"),
            after="or add <Android SDK>/platform-tools to PATH",
            group="Android",
        ),
        Tool(
            "figma",
            "designs for the agent via Figma's desktop MCP server",
            _app("Figma"),
            cask("figma"),
            ready=lambda: _port_open(FIGMA_MCP_PORT),
            after=DESKTOP_HINT,
            group="Design",
        ),
        Tool(
            "node",
            "Node.js 20+ for Pixel Agents",
            _node_20,
            "brew install node" if mac else "",
            group="Optional",
            soft=True,
        ),
        Tool(
            "pixel-agents",
            "pixel office for factory runs and Claude Code agents",
            lambda: has("pixel-agents"),
            "npm install --global pixel-agents",
            group="Optional",
            soft=True,
        ),
        Tool(
            "maestro",
            "multi-step UI flows",
            lambda: has("maestro"),
            "curl -fsSL https://get.maestro.mobile.dev | bash",
            optional=True,
            group="Optional",
        ),
    ]
    overrides = (cfg or SetupConfig()).tools
    out = []
    wanted = {"claude": "claude", "cursor": "cursor-cli"}
    chosen = {wanted[a] for a in (agents if agents is not None else ["claude"])}
    for t in listed:
        if t.name in wanted.values() and t.name not in chosen:
            continue
        if t.name in ("node", "pixel-agents") and not pixel:
            continue
        if t.name == "figma" and figma != "desktop":
            continue  # remote server or no Figma: the desktop app is not needed
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
    for st in p.steps:
        tool = next(t for t in tool_list if t.name == st.tool)
        st.group, st.why, st.soft = tool.group, tool.why, tool.soft
    return p


# tool → (Homebrew name, is a cask); only tools Homebrew installed show up in `brew outdated`
BREW = {
    "git": ("git", False),
    "gh": ("gh", False),
    "node": ("node", False),
    "java": ("temurin@17", True),
    "android-studio": ("android-studio", True),
    "adb": ("android-platform-tools", True),
    "figma": ("figma", True),
}


@dataclass
class Upgrade:
    tool: str
    current: str
    latest: str
    command: str


def outdated(tool_list: list[Tool]) -> list[Upgrade]:
    """Newer versions of installed tools, from Homebrew's local index and npm; auto-updating CLIs are not listed."""
    names = {t.name for t in tool_list if t.present()}
    ups: list[Upgrade] = []
    if has("brew"):
        r = run(["brew", "outdated", "--json=v2"], env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"})
        data = json.loads(r.out) if r.ok and r.out.strip() else {}
        found = {e["name"]: e for kind in ("formulae", "casks") for e in data.get(kind, [])}
        for tool, (name, cask) in BREW.items():
            if tool in names and (e := found.get(name)):
                current = ", ".join(e.get("installed_versions") or [])
                flag = "--cask " if cask else ""
                ups.append(Upgrade(tool, current, str(e.get("current_version", "")), f"brew upgrade {flag}{name}"))
    if "pixel-agents" in names and has("npm"):
        r = run(["npm", "outdated", "--global", "--json", "pixel-agents"])  # exits 1 when something is outdated
        e = (json.loads(r.out) if r.out.strip() else {}).get("pixel-agents")
        if e and e.get("current") != e.get("latest"):
            ups.append(Upgrade("pixel-agents", e["current"], e["latest"], "npm install --global pixel-agents@latest"))
    return ups


Runner = Callable[[str], int]


def shell(command: str) -> int:
    """Run with the user's terminal attached: installers and browser logins are interactive."""
    env = {**os.environ, "PATH": f"{os.environ.get('PATH', '')}{os.pathsep}{LOCAL_BIN}"}  # CLIs installed just now
    return subprocess.call(command, shell=True, env=env)  # noqa: S602 - fixed commands from the tool table or the lead's config


def execute(
    p: Plan,
    confirm: Callable[[Step], bool],
    runner: Runner = shell,
    echo: Callable[[str], None] = print,
    installer: Callable[[Step], int] | None = None,
) -> bool:
    """`installer` runs install steps (quietly, with its own progress); logins always get the terminal via `runner`."""
    ok = True
    failed: set[str] = set()
    for s in p.todo:
        if s.tool in failed:
            continue
        if s.action in ("manual", "missing"):
            echo(f"  manual  {s.tool}  {s.note}" + (f"\n    $ {s.command}" if s.command else ""))
            ok = ok and s.soft
            if s.action == "missing":
                failed.add(s.tool)
            continue
        if not confirm(s):
            echo(f"  skipped  {s.tool} {s.action}")
            ok = ok and s.soft
            continue
        if installer and s.action == "install":
            code = installer(s)
        else:
            echo(f"  $ {s.command}")
            code = runner(s.command)
        if code != 0:
            if s.action != "install" or not installer:  # the installer already reported it, with the log tail
                echo(f"  FAIL  {s.tool} {s.action} failed")
            failed.add(s.tool)
            ok = ok and s.soft
    return ok

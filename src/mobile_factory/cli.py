from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__, adapters, config, doctor, events, metrics, usage, viz, workflow
from . import setup as machine
from . import uninstall as remover
from .config import VizConfig
from .errors import FactoryError, Refused
from .evals import Evals, report
from .gitops import Git
from .guardrail import context as review_ctx
from .guardrail import harvest as harvester
from .guardrail import rules as rl
from .init import init as do_init
from .integrations import github
from .outputs import MODELS
from .pipeline import Engine
from .platforms import make as make_platform
from .platforms.base import snapshot_diff
from .proc import has, which
from .proc import run as proc_run
from .state import RunStore
from .wizard import Wizard, store_secret, write_local

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Mobile Factory: ticket -> verified draft PR, with gates."
)
secrets_app = typer.Typer(no_args_is_help=True, help="Per-machine secrets file (one place for every token).")
android_app = typer.Typer(no_args_is_help=True, help="Drive the Android app on the attached device/emulator.")
guard_app = typer.Typer(
    no_args_is_help=True, help="Code review guardrail: owner taste, tribal knowledge, AI-slop detectors."
)
eval_app = typer.Typer(no_args_is_help=True, help="Replay past fixed tickets to measure the factory.")
app.add_typer(secrets_app, name="secrets")
app.add_typer(android_app, name="android")
app.add_typer(guard_app, name="guardrail")
viz_app = typer.Typer(
    no_args_is_help=True,
    help="Try the visualiser on its own, no factory run needed: status, start, stop, demo.",
)
app.add_typer(viz_app, name="viz")
app.add_typer(eval_app, name="eval")

RunOpt = Annotated[str | None, typer.Option("--run", help="Run id (default: the active run).")]


def _lc() -> config.LoadedConfig:
    lc = config.load()
    github.activate(lc.cfg.vcs.github_account)
    return lc


def _engine(run: str | None = None) -> Engine:
    return Engine.load(_lc(), run)


_MARKS = {
    "ok": ("✓", "green"),
    "warn": ("!", "yellow"),
    "warning:": ("!", "yellow"),
    "rejected:": ("✗", "red"),
    "FAIL": ("✗", "red"),
    "MISSING": ("✗", "red"),
    "install": ("↓", "cyan"),
    "login": ("→", "cyan"),
    "manual": ("•", "yellow"),
    "delete": ("✗", "red"),
    "edit": ("~", "yellow"),
    "note": ("•", "cyan"),
    "skipped": ("○", "yellow"),
}
_SYMBOL_ONLY = {"ok", "warn", "warning:", "rejected:", "FAIL", "MISSING"}
_STATUS = re.compile(r"^(\s*)(" + "|".join(map(re.escape, _MARKS)) + r")(\s+)(.*)$")


def _fmt(line: str, hints: bool = False) -> str:
    """Colour one output line: `== Section` headers, `ok/warn/FAIL …` status lines, `DOCTOR: …`, indented hints."""
    if line.startswith("== "):
        title = line[3:]
        return typer.style(f"▌ {title}", fg="cyan", bold=True) + "\n" + typer.style("─" * 60, dim=True)
    if m := _STATUS.match(line):
        indent, word, gap, rest = m.groups()
        sym, color = _MARKS[word]
        name, _, detail = rest.partition("  ")
        action = "" if word in _SYMBOL_ONLY else typer.style(word, fg=color) + gap
        text = f"{indent}{typer.style(sym, fg=color, bold=True)} {action}{name}"
        return text + (" " + typer.style(detail.strip(), dim=True) if detail.strip() else "")
    if line.startswith("DOCTOR: "):
        good = line.endswith("PASS")
        return "\n" + typer.style(f"{'✓' if good else '✗'} {line}", fg="green" if good else "red", bold=True)
    if hints and line.startswith("  · "):  # key/value row: cyan key column, plain value
        key, _, value = line[4:].partition("  ")
        return f"  {typer.style(f'{key:<14}', fg='cyan', bold=True)}{value.strip()}"
    if hints and line.startswith("  "):
        return typer.style(line, dim=True)
    return line


_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# On-screen labels for long work in the visualiser; the terminal keeps the full phase name.
_SHORT = {
    "Listing merged PRs": "PR list",
    "Reading review comments": "comments",
    "Resolving review threads": "threads",
    "Tallying review chunks": "tally",
    "Writing taste rules": "taste rules",
}


def _watch(root: Path | None, name: str) -> viz.Session | None:
    """Long CLI work (harvest, distill) as its own session in the developer's visualiser, if one is on."""
    if root is None:
        return None
    v = viz.make(config.load(root).cfg.viz)
    return None if isinstance(v, viz.NullVisualizer) else viz.Session(v, root, name)


class _Progress:
    """Live `▸ phase  ██████░░░░  12/40` line per phase, replaced by `✓ phase (40)` when the next one starts."""

    WIDTH = 24

    def __init__(self, office: viz.Session | None = None) -> None:
        self.live = sys.stdout.isatty()
        self.phase, self.done, self.total, self.frame = "", 0, 0, 0
        self.started = time.monotonic()
        self.office = office
        if office:
            office.begin()

    def _took(self) -> str:
        s = int(time.monotonic() - self.started)
        return f"{s // 60}:{s % 60:02d}" if s else ""

    def __call__(self, phase: str, done: int, total: int) -> None:
        if phase != self.phase:
            self.close()
            self.phase, self.started = phase, time.monotonic()
            if self.office:
                self.office.step(phase, _SHORT.get(phase, ""))
            if not self.live:
                typer.echo(f"  {phase}…")
        self.done, self.total = done, total
        if not self.live:
            return
        self.frame += 1
        if total:
            fill = self.WIDTH * done // total
            bar = typer.style("█" * fill, fg="cyan") + typer.style("░" * (self.WIDTH - fill), dim=True)
            tail = f"  {done}/{total}  {typer.style(f'{100 * done // total}%', dim=True)}"
        else:
            bar, tail = typer.style("working…", dim=True), ""
        if self.office:
            self.office.pulse()
        spin = typer.style(_SPIN[self.frame % len(_SPIN)], fg="cyan", bold=True)
        took = typer.style(f"  {self._took()}", dim=True) if self._took() else ""
        typer.echo(f"\r\033[2K  {spin} {phase}  {bar}{tail}{took}", nl=False)

    def clear(self) -> None:
        """Drop the live line without a ✓ (the phase did not finish)."""
        if self.live and self.phase:
            typer.echo("\r\033[2K", nl=False)
        self.phase = ""
        self.leave("stopped")

    def leave(self, outcome: str = "done") -> None:
        if self.office:
            self.office.end(outcome)
            self.office = None

    def close(self) -> None:
        if not self.phase:
            return
        if self.live:
            typer.echo("\r\033[2K", nl=False)
        if self.office:
            self.office.step_done(self.phase)
        facts = " · ".join(x for x in (str(self.total) if self.total else "", self._took()) if x)
        typer.echo(
            f"  {typer.style('✓', fg='green', bold=True)} {self.phase}{typer.style(f' ({facts})', dim=True) if facts else ''}"
        )
        self.phase = ""


def _install_quietly(step: machine.Step) -> int:
    """Installer output goes to a log; the terminal shows one spinner line, then ✓ or ✗ with the log tail."""
    log = config.secrets_path().parent / "logs" / f"{step.action}-{step.tool}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    live, start = sys.stdout.isatty(), time.monotonic()
    env = {**os.environ, "NONINTERACTIVE": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
    verb, done_verb = ("upgrading", "upgraded") if step.action == "upgrade" else ("installing", "installed")
    if not live:
        typer.echo(f"  {verb} {step.tool}…")
    with log.open("w") as fh:
        fh.write(f"$ {step.command}\n")
        fh.flush()
        proc = subprocess.Popen(  # noqa: S602 - fixed commands from the tool table or the lead's config
            step.command, shell=True, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env
        )
        i = 0
        while proc.poll() is None:
            if live:
                took = int(time.monotonic() - start)
                spin = typer.style(_SPIN[i % len(_SPIN)], fg="cyan", bold=True)
                typer.echo(
                    f"\r\033[2K  {spin} {verb} {step.tool}  {typer.style(f'{took // 60}:{took % 60:02d}', dim=True)}",
                    nl=False,
                )
            i += 1
            time.sleep(0.1)
    took = int(time.monotonic() - start)
    if live:
        typer.echo("\r\033[2K", nl=False)
    if proc.returncode == 0:
        typer.echo(
            f"  {typer.style('✓', fg='green', bold=True)} {done_verb} {step.tool} {typer.style(f'({took}s)', dim=True)}"
        )
        return 0
    tail = log.read_text(errors="ignore").strip().splitlines()[-6:]
    typer.echo(
        f"  {typer.style('✗', fg='red', bold=True)} {verb} {step.tool} failed {typer.style(f'({took}s)', dim=True)}"
    )
    for line in tail:
        typer.echo(typer.style(f"    {line[:160]}", dim=True))
    typer.echo(typer.style(f"    log: {log}\n    run it yourself: {step.command}", dim=True))
    return proc.returncode


def _run_watched(
    cmds: list[list[str]],
    cwd: Path,
    logs: list[Path],
    stage: Callable[[], tuple[str, int, int]],
    *,
    timeout: int = 1800,
    office: viz.Session | None = None,
) -> int:
    """Run headless commands side by side, each into its log; `stage()` (read from files they write) drives the
    progress line. Returns 0, or the first non-zero exit code."""
    bar = _Progress(office)
    start = time.monotonic()
    procs: list[subprocess.Popen[bytes]] = []
    handles = [log.open("w") for log in logs]
    try:
        for cmd, fh in zip(cmds, handles, strict=True):
            procs.append(
                subprocess.Popen(
                    [which(cmd[0]) or cmd[0], *cmd[1:]],
                    cwd=cwd,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
            )
        while any(p.poll() is None for p in procs):
            if time.monotonic() - start > timeout:
                for p in procs:
                    p.kill()
                bar.clear()
                return 124
            bar(*stage())
            time.sleep(0.2)
    except BaseException:
        for p in procs:
            p.kill()
        bar.clear()
        raise
    finally:
        for fh in handles:
            fh.close()
    code = next((p.returncode for p in procs if p.returncode), 0)
    if code == 0:
        bar(*stage())
        bar.close()
        bar.leave()
    else:
        bar.clear()
    return code


def _home(p: Path) -> str:
    return str(p).replace(str(Path.home()), "~")


def _harvest_summary(meta: dict[str, Any]) -> str:
    owners = ", ".join(meta.get("owners") or [])
    return (
        f"  ok  harvested  ({meta['comments']} comments on {meta['prs_with_owner_comments']} PRs,"
        f" {meta['changes_requested']} from change requests)\n"
        f"  PRs: {meta.get('prs_new', meta['prs_scanned'])} read, {meta.get('prs_already_processed', 0)} already"
        f" processed{' (full re-extraction)' if meta.get('full') else ''} · owners: {owners}"
        f" (from {meta.get('owner_source', '?')})"
    )


def _harvest(lc: config.LoadedConfig, **kw: Any) -> dict[str, Any]:
    bar = _Progress(_watch(lc.root, "harvest"))
    try:
        meta = harvester.harvest(
            lc.root, Git(lc.root).remote_repo(lc.cfg.vcs.remote), lc.state_dir / "data", progress=bar, **kw
        )
    except BaseException:
        bar.clear()  # failed mid-phase: no ✓
        raise
    bar.close()
    bar.leave()
    return meta


def _say(text: str, hints: bool = False) -> None:
    typer.echo("\n".join(_fmt(ln, hints) for ln in text.split("\n")))


def _human_only(action: str) -> None:
    if (
        os.environ.get("CI")
        or os.environ.get("FACTORY_NONINTERACTIVE")
        or not (sys.stdin.isatty() and sys.stdout.isatty())
    ):
        raise Refused(f"{action} needs a human at an interactive terminal")


# ---------- setup ----------


@app.command()
def version() -> None:
    """Print the version."""
    _say(__version__)


def _setup(yes: bool, optional: bool) -> bool:
    try:
        loaded = config.load().cfg
        cfg, figma, agents = loaded.setup, config.figma_mode(loaded), list(loaded.agents.use)
        pixel = loaded.viz.tool if loaded.viz.pixel_agents else ""
    except FactoryError:
        cfg, figma, agents, pixel = None, "desktop", None, ""
    p = machine.plan(machine.tools(cfg, figma, agents, pixel), optional=optional)
    typer.echo(_render_plan(p))
    if not p.todo:
        if sys.stdin.isatty() and sys.stdout.isatty():
            _offer_upgrades(machine.tools(cfg, figma, agents, pixel), yes)
        _say("\n  ok  machine ready", hints=True)
        return True
    if os.environ.get("CI") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        _say(
            "\n  warn  run `factory setup` in your own terminal: installs and browser logins need you there", hints=True
        )
        return False
    typer.echo()

    def confirm(s: machine.Step) -> bool:
        if yes:
            return True
        typer.echo(typer.style(f"    $ {s.command}", dim=True))
        return _yes(f"{'Install' if s.action == 'install' else 'Log in to'} {s.tool}?", True)

    ok = machine.execute(p, confirm, echo=lambda t: _say(t, hints=True), installer=_install_quietly)
    _offer_upgrades(machine.tools(cfg, figma, agents, pixel), yes)
    left = "  ok  machine ready" if ok else "  warn  steps left above: finish them, then `factory setup` again"
    _say("\n" + left, hints=True)
    return ok


def _offer_upgrades(tool_list: list[machine.Tool], yes: bool = False) -> None:
    """List newer versions of installed tools and upgrade them on a yes (quiet, one spinner line each)."""
    ups = machine.outdated(tool_list)
    if not ups:
        _say("\n  ok  tools up to date  (Homebrew's last index; Claude Code and Cursor update themselves)", hints=True)
        return
    typer.echo("\n" + typer.style("  Updates available", bold=True))
    for u in ups:
        arrow = typer.style("↑", fg="cyan", bold=True)
        typer.echo(f"    {arrow} {u.tool:<15} {typer.style(u.current, dim=True)} → {typer.style(u.latest, fg='cyan')}")
    if not (yes or (sys.stdin.isatty() and _yes(f"Upgrade {'it' if len(ups) == 1 else f'all {len(ups)}'} now?", True))):
        _say("    later: `factory setup` or `factory doctor` offers them again", hints=True)
        return
    for u in ups:
        _install_quietly(machine.Step(u.tool, "upgrade", u.command))


_PLAN_MARK = {"ok": ("✓", "green"), "install": ("↓", "cyan"), "login": ("→", "cyan"), "manual": ("•", "yellow")}
_PLAN_MARK["missing"] = ("✗", "red")
_PLAN_WORD = {"install": "install", "login": "log in", "manual": "manual step", "missing": "missing"}


def _render_plan(p: machine.Plan) -> str:
    """Tools grouped in setup order; ✓ ready, or what is left (install · log in) with the reason, dimmed."""
    tools: dict[str, list[machine.Step]] = {}
    for s in p.steps:
        tools.setdefault(s.tool, []).append(s)
    lines: list[str] = []
    group: str | None = None
    for name, steps in tools.items():
        if steps[0].group != group:
            group = steps[0].group
            lines.append(("" if not lines else "\n") + typer.style(f"  {group or 'Other'}", bold=True))
        todo = [s for s in steps if s.action != "ok"]
        sym, color = _PLAN_MARK[todo[0].action if todo else "ok"]
        what = typer.style(" · ".join(_PLAN_WORD[s.action] for s in todo), fg=color) + "  " if todo else ""
        reason = next((s.note for s in todo if s.action in ("manual", "missing") and s.note), steps[0].why)
        lines.append(f"    {typer.style(sym, fg=color, bold=True)} {name:<15} {what}{typer.style(reason, dim=True)}")
    return "\n".join(lines)


@app.command()
def setup(
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without asking per tool."),
    optional: bool = typer.Option(False, help="Also install optional tools (Maestro)."),
) -> None:
    """Install and log in to what the factory needs on this machine: git, gh, twg, JDK, Android tools, Figma."""
    ok = _setup(yes, optional)
    if ok:
        _say("  next: `factory doctor` in your project", hints=True)
    raise typer.Exit(0 if ok else 1)


class _Quit(typer.Abort):
    """`q` at any prompt."""


def _answer(raw: object) -> str:
    text = str(raw).strip()
    if text.lower() == "q":
        raise _Quit()
    return text


def _yes(question: str, default: bool) -> bool:
    d = "y" if default else "n"
    while True:
        raw = _answer(typer.prompt(_q(f"{question} (y/n)", d), default=d, show_default=False)).lower()
        if raw in ("y", "yes", "n", "no"):
            return raw.startswith("y")


def _rollback_paths(root: Path) -> list[Path]:
    exclude = Path(
        proc_run(["git", "rev-parse", "--git-path", "info/exclude"], root).out.strip() or ".git/info/exclude"
    )
    secrets = {config.secrets_path()}
    with contextlib.suppress(FactoryError):
        secrets.add(config.secrets_path(config.load(root).cfg.secrets_file))
    return [
        config.config_path(root),
        config.local_path(root),
        exclude if exclude.is_absolute() else root / exclude,
        *secrets,
    ]


@contextlib.contextmanager
def _nothing_saved_on_quit(root: Path) -> Iterator[None]:
    """`q` or Ctrl+C: put every file the wizard touches back the way it was."""
    saved = {p: p.read_bytes() if p.is_file() else None for p in _rollback_paths(root)}
    home = config.state_dir(root)
    had_dir = home.exists()
    try:
        yield
    except (typer.Abort, KeyboardInterrupt):
        for p, data in saved.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        if not had_dir:
            shutil.rmtree(home, ignore_errors=True)
        _say("\nFAIL  quit  (nothing saved)")
        raise typer.Exit(1) from None


def _q(question: str, default: str = "") -> str:
    tail = " " + typer.style(f"[{default}]", fg="cyan") if default else ""
    return typer.style("? ", fg="magenta", bold=True) + typer.style(question, bold=True) + tail


def _choose(question: str, options: list[str], default: int = 0) -> int:
    typer.echo(_q(question))
    for i, o in enumerate(options, 1):
        if i == default + 1:
            typer.echo(typer.style(f" ▸ {i}) {o}  (default)", fg="cyan", bold=True))
        else:
            typer.echo(f"   {i}) {o}")
    while True:
        raw = _answer(typer.prompt(typer.style("    choose", dim=True), default=str(default + 1)))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        _say(f"  pick 1-{len(options)}, or q to quit", hints=True)


class _TerminalPrompter:
    def say(self, text: str) -> None:
        _say(text, hints=True)

    def ask(self, question: str, default: str = "") -> str:
        return _answer(typer.prompt(_q(question, default), default=default, show_default=False))

    def secret(self, question: str) -> str:
        return _answer(getpass.getpass(_q(question) + ": "))

    def choose(self, question: str, options: list[str], default: int = 0) -> int:
        return _choose(question, options, default)

    def confirm(self, question: str, default: bool = True) -> bool:
        if question.startswith("\n"):
            typer.echo()
        return _yes(question.lstrip("\n"), default)


_DISTILL_TOOLS = (
    "Bash(jq:*),Bash(sed:*),Bash(grep:*),Bash(git:*),Bash(wc:*),Bash(head:*),Bash(cat:*),Bash(ls:*),"
    "Read,Write,Edit,Grep,Glob,Agent,Task"
)


# Tools a headless step may use: read and edit the repo, run the build and the device through the factory CLI.
_STEP_TOOLS = (
    "Read,Write,Edit,Grep,Glob,Agent,Task,Bash(factory:*),Bash(./gradlew:*),Bash(gradle:*),Bash(adb:*),Bash(git:*),"
    "Bash(gh:*),Bash(twg:*),Bash(jq:*),Bash(sed:*),Bash(grep:*),Bash(rg:*),Bash(find:*),Bash(ls:*),Bash(cat:*),"
    "Bash(head:*),Bash(tail:*),Bash(wc:*),Bash(mkdir:*)"
)


def _agent_cmd(
    agent: str, prompt: str, model: str, state: Path, *, agents: Path | None = None, tools: str = _DISTILL_TOOLS
) -> list[str]:
    """One headless agent session; `agents` defines inline subagents it may start (Claude Code)."""
    pick = ["--model", model] if model and model != "inherit" else []
    if agent == "claude":
        extra = ["--agents", str(agents)] if agents else []
        return [
            "claude", "-p", prompt, "--permission-mode", "acceptEdits",
            "--allowedTools", tools, "--add-dir", str(state), *extra, *pick,
        ]  # fmt: skip
    return [agent, "-p", prompt, "--force", "--output-format", "text", *pick]


_CLIS = {"claude": ["claude"], "cursor": ["agent", "cursor-agent"]}


def _agent_cli(lc: config.LoadedConfig) -> str:
    """First installed CLI of the agents this developer chose in `factory init`, in their order."""
    return next((cli for a in lc.cfg.agents.use for cli in _CLIS[a] if has(cli)), "")


def _distill(lc: config.LoadedConfig, refresh: bool = False) -> bool:
    """Taste rules from the harvest in one headless session: it starts a tally subagent per chunk, all at once
    (as many as agents.max_parallel allows), then merges their tallies into the rules. True when written."""
    agent = _agent_cli(lc)
    if not agent:
        _say(f"  warning: no {' or '.join(lc.cfg.agents.use)} CLI installed; see Machine tools above", hints=True)
        return False
    data = lc.state_dir / "data"
    source = "reviews-new.jsonl" if refresh else "reviews.jsonl"
    taste = lc.path(lc.cfg.guardrail.taste)
    width = lc.cfg.agents.max_parallel if lc.cfg.agents.parallel else 1
    chunks = harvester.chunks(data, source=source, parallel=width)
    if not chunks:
        _say("  warning: nothing to tally", hints=True)
        return False
    for old in data.glob("tally-*.json"):
        old.unlink()
    target = "cursor" if agent != "claude" else "claude"
    agents = None
    if agent == "claude":  # Cursor uses the factory-learn-tally subagent `factory install` put in ~/.cursor/agents
        agents = data / "distill-agents.json"
        _, body = adapters._split(adapters.subagent_text("learn-tally", "inherit"))
        tally = {"description": "Tally one line range of the reviews", "prompt": body}
        agents.write_text(
            json.dumps({"factory-learn-tally": {**tally, "model": lc.cfg.agents.model_for("learn-tally", target)}})
        )
    mode = (
        f"REFRESH: {taste} exists and already covers every older comment; only new comments are tallied."
        " Update the rules per step 6 (bump evidence, next free id for new rules, never renumber or reuse ids).\n"
        if refresh
        else ""
    )
    prompt = (
        mode + f"The harvest is already done. Write only {taste} (the subagents write the tally files). Headless: use"
        " the Read/Write tools for files and one simple command per Bash call (no pipes, loops or `;` chains).\n\n"
        + harvester.parallel_plan(data, source, parallel=width)
        + "\n\n"
        + resources.files("mobile_factory.skills").joinpath("guardrail-learn.md").read_text()
    )
    cmd = _agent_cmd(agent, prompt, lc.cfg.agents.model_for("guardrail-learn", target), lc.state_dir, agents=agents)
    t0 = time.time()

    def written() -> bool:
        return taste.is_file() and taste.stat().st_mtime >= t0

    def stage() -> tuple[str, int, int]:
        done = len(list(data.glob("tally-*.json")))
        if written() or done >= len(chunks):
            return "Writing taste rules", 0, 0
        return "Tallying review chunks", done, len(chunks)

    log = data / "distill.log"
    _say(f"  distilling with {agent}: {len(chunks)} tally subagents in parallel  (log: {_home(log)})", hints=True)
    # with live hooks the office shows this session and its subagents by itself: no extra character
    office = None if viz.make(lc.cfg.viz).live_hooks() else _watch(lc.root, "distill")
    code = _run_watched([cmd], lc.root, [log], stage, office=office)
    if written():
        harvester.mark_distilled(data)
        _say(f"  ok  taste rules  ({_home(taste)}: on your machine only)", hints=True)
        return True
    _say(f"  warning: {agent} did not write {_home(taste)} (exit {code}); see the log", hints=True)
    return False


OFFICE_HINT = "`factory viz start`"


OFFICE_HOOKS_HINT = (
    "open the office link above, then Settings → Instant Detection (Hooks) → on (live agents and subagents)"
)


def _taste_status(lc: config.LoadedConfig) -> tuple[int, str]:
    """(rule count, last harvest date) of the existing taste rules."""
    rules = sum(1 for ln in lc.path(lc.cfg.guardrail.taste).read_text().splitlines() if ln.startswith("### R"))
    meta = lc.state_dir / "data" / "harvest_meta.json"
    date = json.loads(meta.read_text()).get("harvested_at", "") if meta.is_file() else ""
    return rules, date


def _start_office(root: Path, ask: bool = True, prefs: VizConfig | None = None) -> viz.Office | None:
    """Bring the developer's visualiser up the way they want it (settings, layout, live hooks) and say where it is."""
    prefs = prefs or config.load(root).cfg.viz
    v = viz.make(prefs)
    if isinstance(v, viz.NullVisualizer):
        return None
    for note in v.prepare(root, prefs):
        _say(f"  ok  {note}", hints=True)
    if v.restart_needed(prefs):
        if not ask or _yes(f"Restart the {v.title} so it turns on live hooks?", True):
            v.stop()
        else:
            _say("  warning: " + OFFICE_HOOKS_HINT, hints=True)
    if live := v.offices():
        _say(f"  ok  {v.title} running  {live[0].url}", hints=True)
        return live[0]
    if not v.installed():
        _say(f"  warning: {v.name} is not installed yet: run `factory setup`", hints=True)
        return None
    if ask and not _yes(f"Start the {v.title} now? (opens your browser, keeps running in the background)", True):
        _say(f"  later: `factory viz start`, or {OFFICE_HINT}", hints=True)
        return None
    bar = _Progress()
    bar(f"Starting the {v.title}", 0, 0)
    started = v.start(root, config.secrets_path().parent / "logs" / f"{v.name}.log")
    bar.clear()
    if not started:
        _say(f"  warning: the {v.title} did not start; see ~/.config/mobile-factory/logs/{v.name}.log", hints=True)
        return None
    stop = f"  · stop it with: kill {started.pid}" if started.pid else ""
    _say(f"  ok  {v.title} running  {started.url}{stop}", hints=True)
    if prefs.pixel_hooks and v.supports_live_hooks:
        for _ in range(25):  # the tool installs its own hook right after it starts
            if v.live_hooks():
                break
            time.sleep(0.2)
        _say("  ok  live hooks on" if v.live_hooks() else "  warning: " + OFFICE_HOOKS_HINT, hints=True)
    return started


def _optional_extras(root: Path) -> None:
    """Extras in the order you would want them: watch (office), then tools, then the long taste run."""
    lc = config.load(root)
    has_taste = lc.path(lc.cfg.guardrail.taste).is_file()
    saved = config.read_yaml(config.local_path(root))
    maestro_off = bool(((saved.get("setup") or {}).get("tools") or {}).get("maestro", {}).get("skip"))
    maestro = not has("maestro") and not maestro_off
    pixel = lc.cfg.viz.pixel_agents
    pixel_answered = "pixel_agents" in (saved.get("viz") or {})
    _say("\n== Extend initialisation", hints=True)
    parts = iter(range(1, 10))

    def part(title: str) -> None:
        typer.echo("\n" + typer.style(f"  {next(parts)} · {title}", bold=True))

    part("Visualisation (optional)")
    if not pixel and pixel_answered:
        _say("  ok  Pixel office off  (turn it on: `viz: {pixel_agents: true}` in your local.yaml)", hints=True)
    elif not pixel:
        _say(
            "    Claude Office shows every agent as a character at its own desk: your Claude Code sessions,\n"
            "    their subagents, and each factory run and its steps. Runs locally, no account needed",
            hints=True,
        )
        if _yes("Watch long-running work in the office?", False):
            _say(
                "    live hooks let the office see your Claude Code sessions and subagents as they work"
                " (adds its hook to ~/.claude/settings.json)",
                hints=True,
            )
            hooks = _yes("Turn on live hooks?", True)
            write_local(lc.root, {"viz": {"pixel_agents": True, "pixel_hooks": hooks}})
            pixel = True
            tool = config.load(lc.root).cfg.viz.tool
            wanted = machine.VIZ_NEEDS.get(tool, ())
            extra = machine.plan(
                [t for t in machine.tools(lc.cfg.setup, config.figma_mode(lc.cfg), [], tool) if t.name in wanted]
            )
            if extra.todo:
                typer.echo(_render_plan(extra))
                machine.execute(extra, lambda _s: True, echo=lambda t: _say(t, hints=True), installer=_install_quietly)
        else:
            write_local(lc.root, {"viz": {"pixel_agents": False}})  # remembered: not asked again
            _say(
                f"    skipped: turn it on any time with `viz: {{pixel_agents: true}}` in {_home(config.local_path(lc.root))}",
                hints=True,
            )
    if pixel:
        _start_office(lc.root)

    if maestro:
        part("UI flows (optional)")
        _say(
            "    maestro replays UI flows (flows/ in the factory home) when checking a fix on the emulator", hints=True
        )
        if not _yes("Install Maestro now?", False):
            write_local(lc.root, {"setup": {"tools": {"maestro": {"skip": True}}}})  # remembered: not asked again
            _say("    skipped: remove `setup.tools.maestro` from your local.yaml to be asked again", hints=True)
        else:
            tools = [t for t in machine.tools(lc.cfg.setup, config.figma_mode(lc.cfg)) if t.name == "maestro"]
            machine.execute(
                machine.plan(tools, optional=True),
                lambda _s: True,
                echo=lambda t: _say(t, hints=True),
                installer=_install_quietly,
            )

    part("Taste rules")
    full = False
    if has_taste:
        rules, date = _taste_status(lc)
        _say(f"  ok  taste rules  ({rules} rules{f', harvested {date}' if date else ''})", hints=True)
        pick = _choose(
            "What should happen to them?",
            [
                "Keep them",
                "Refresh with PRs merged since the last harvest",
                "Rebuild from scratch (invalidate harvested data)",
            ],
            0,
        )
        if pick == 0:
            return
        full = pick == 2
    else:
        _say("    teach the guardrail your code owners' review habits, learned from merged PRs", hints=True)
    if not _agent_cli(lc):
        _say(
            f"  warning: {' / '.join(lc.cfg.agents.use)} CLI not installed yet: you can harvest now,"
            " distill once it is (`factory setup`)",
            hints=True,
        )
    if not (has_taste or _yes("Harvest past PR reviews now? (takes a few minutes)", True)):
        return
    try:
        meta = _harvest(lc, full=full)
    except FactoryError as e:
        _say(f"  warning: harvest skipped: {' '.join(str(e).split())[:160]}", hints=True)
        return
    _say(_harvest_summary(meta), hints=True)
    data = lc.state_dir / "data"
    if has_taste and not full:
        if not (data / harvester.DISTILLED).is_file() and meta.get("prs_new") == 0:
            harvester.mark_distilled(data)  # rules built before this record existed, and nothing new since
        new = harvester.undistilled(data)
        if not new:
            _say("  ok  taste rules up to date  (no new review comments since they were built)", hints=True)
            return
        _say(f"    {len(new)} new review comments since the rules were built: only those are tallied", hints=True)
    verb = "Update the taste rules" if has_taste else "Distill them into taste rules"
    ask = bool(_agent_cli(lc)) and _yes(f"{verb} with your agent now?", True)
    if ask and pixel:
        _start_office(lc.root)  # the distill is the long agent run: offer to watch it
    if not (ask and _distill(lc, refresh=has_taste and not full)):
        _say("  later: ask your agent to run the `guardrail-learn` skill", hints=True)


@app.command()
def init(
    reconfigure: bool = typer.Option(False, help="Ask the project questions again and rewrite factory.yaml."),
    skip_setup: bool = typer.Option(False, help="Do not check or install machine tools afterwards."),
    defaults: bool = typer.Option(False, help="Non-interactive: write factory.yaml from detected values only."),
) -> None:
    """Guided setup: project settings (first time), then your Jira, GitHub, Slack and Figma access, then machine tools."""
    root = config.repo_root()
    if defaults:
        for line in do_init(root, force=reconfigure):
            _say(f"  {line}")
        _say(f"wrote {config.config_path(root)}")
        return
    _human_only("factory init")
    _say("  type q at any prompt to quit without saving", hints=True)
    with _nothing_saved_on_quit(root):
        res = Wizard(root, _TerminalPrompter()).run(reconfigure=reconfigure)
        if not skip_setup:
            _say("\n== Machine tools")
            _setup(yes=False, optional=False)
        _optional_extras(root)
    if res.todo:
        _say("\nstill to do:\n" + "\n".join(f"  - {t}" for t in res.todo))
    _say("\n  next: `factory doctor`, then `factory install` (nothing to commit: the repo is untouched)", hints=True)


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    force: bool = typer.Option(False, help="Remove even if a run is still open."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would be removed."),
    keep_home: bool = typer.Option(False, "--keep-home", help="Keep this repo's settings, runs and taste rules."),
    global_: bool = typer.Option(
        False, "--global", help="Also remove the factory skills and subagents from ~/.claude and ~/.cursor."
    ),
) -> None:
    """Remove the factory for this repo: its factory home, old in-repo files and, with --global, the user-level skills."""
    try:
        root = config.find_root()
    except FactoryError:
        root = config.repo_root()  # half-removed repo: clean what is left
    rm = remover.plan(root, home=not keep_home, global_=global_)
    _say(rm.render(root))
    if not (rm.delete or rm.edit) or dry_run:
        return
    if rm.open_runs and not force:
        raise FactoryError(f"open runs {', '.join(rm.open_runs)}: finish or `factory abort` them, or pass --force")
    if not yes:
        _human_only("factory uninstall")
        if not _yes("Remove all of the above?", False):
            raise typer.Exit(1)
    remover.apply(root, rm)
    _say("ok  removed  (machine-wide items stay: the `factory` command and ~/.config/mobile-factory/secrets.env)")


@app.command()
def install(target: Annotated[str, typer.Option(help="claude | cursor | all")] = "all") -> None:
    """Install the factory skills, subagents and MCP servers for Claude Code and/or Cursor, in your home folder only."""
    lc = _lc()
    old = remover.plan(lc.root, home=False)  # integration files older versions put into the repo
    if old.delete or old.edit:
        _say("  moving the factory out of the repo:", hints=True)
        _say(old.render(lc.root))
        remover.apply(lc.root, old)
    targets = ["claude", "cursor"] if target == "all" else [target]
    for t in targets:
        if t not in ("claude", "cursor"):
            raise FactoryError(f"unknown target {t}")
        for p in adapters.install(lc, t):  # type: ignore[arg-type]
            _say(f"  ok  {t}  ({_home(p)})", hints=True)
    _say("  ok  repo untouched  (nothing to commit)", hints=True)


@app.command(name="doctor")
def doctor_cmd(offline: bool = typer.Option(False, help="Skip network checks.")) -> None:
    """Check tools, secrets, auth, device and config."""
    lc = _lc()
    text, ok = doctor.render(doctor.checks(lc, online=not offline))
    _say(text)
    if not offline and sys.stdin.isatty() and sys.stdout.isatty():
        c = lc.cfg
        _offer_upgrades(
            machine.tools(c.setup, config.figma_mode(c), list(c.agents.use), c.viz.tool if c.viz.pixel_agents else "")
        )
    raise typer.Exit(0 if ok else 1)


@secrets_app.command("set")
def secrets_set(name: str) -> None:
    """Store a secret (typed, never echoed) in the per-machine secrets file."""
    _human_only("setting a secret")
    value = getpass.getpass(f"{name}: ")
    if not value:
        raise FactoryError("empty value, nothing stored")
    _say(f"stored {name} in {store_secret(name, value, _secrets_file())}")


@secrets_app.command("list")
def secrets_list() -> None:
    """List secret names (values are never printed) and which ones factory.yaml needs."""
    path = config.secrets_path(_secrets_file())
    have = config.read_env_file(path)
    try:
        needed = config.env_refs(_lc().raw)
    except FactoryError:
        needed = set()
    for name in sorted(have.keys() | needed):
        state = "set (file)" if name in have else ("set (env)" if name in os.environ else "MISSING")
        _say(f"  {name:<28} {state}{'' if name in needed else '  (unused)'}")
    _say(f"file: {path}")


def _secrets_file() -> str:
    try:
        return str(config.load().cfg.secrets_file)
    except FactoryError:
        return config.DEFAULT_SECRETS_FILE


@app.command(name="exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_cmd(ctx: typer.Context) -> None:
    """Run a command with the secrets loaded into its environment, e.g. `factory exec -- claude`."""
    if not ctx.args:
        raise FactoryError("usage: factory exec -- <command> [args]")
    env = config.environment(_secrets_file())
    with contextlib.suppress(FactoryError):  # outside a factory repo: plain environment
        env.update(github.account_env(config.load().cfg.vcs.github_account))
    os.execvpe(ctx.args[0], ctx.args, env)  # noqa: S606 - the user's own command


# ---------- runs ----------


@app.command()
def run(
    ticket: str,
    base: Annotated[
        str | None, typer.Option(help="Base branch for this run (required when base_branch is 'ask').")
    ] = None,
    autonomy: Annotated[int | None, typer.Option(min=0, max=4, help="Autonomy ceiling for this run (0-4).")] = None,
    workflow_name: Annotated[
        str | None,
        typer.Option("--workflow", help="Force a workflow instead of detecting it (bugfix, task, feature, …)."),
    ] = None,
) -> None:
    """Start a run for a ticket and advance to the first agent step or gate."""
    if workflow_name and workflow_name not in workflow.all_workflows():
        raise FactoryError(f"unknown workflow {workflow_name}; known: {', '.join(workflow.all_workflows())}")
    lc = _lc()
    open_run = next((r for r in RunStore(lc.runs_dir).all() if r.ticket == ticket and not r.finished), None)
    if open_run:
        eng = Engine(lc, open_run)
        if sys.stdout.isatty():
            _say(f"  picking up {open_run.id} where it stopped", hints=True)
    else:
        eng = Engine.start(lc, ticket, ceiling=autonomy, base=base, workflow_name=workflow_name)
    eng.advance()
    if sys.stdout.isatty() and not os.environ.get("CI"):
        _wizard(eng)
    else:
        _show(eng)


def _wizard(eng: Engine) -> None:
    """Drive the run from the terminal: agent steps run headless, gates ask here, `q` or Ctrl+C stops. Every step is
    saved, so `factory run <KEY>` again picks up where it stopped."""
    agent = _agent_cli(eng.lc)
    try:
        while not eng.st.finished:
            _show(eng, brief=True)
            node = eng.node()
            if eng.st.status == "waiting_gate":
                _gate_here(eng, node.gate or "")
            elif node.kind == "agent" and eng.st.status == "waiting_agent":
                if not agent:
                    _say(
                        f"  warning: no {' or '.join(eng.cfg.agents.use)} CLI: run the step from your agent", hints=True
                    )
                    return
                _agent_step(eng, agent)
            else:
                eng.advance()
        _show(eng)
    except (KeyboardInterrupt, _Quit, typer.Abort):
        eng.save()
        typer.echo()
        _say(f"  ○ paused at {eng.node().title}. Pick up with: factory run {eng.st.ticket}", hints=True)
        raise typer.Exit(0) from None


def _gate_here(eng: Engine, gate: str) -> None:
    rec = eng.st.gates[gate]
    for line in eng.gate_summary(gate).splitlines()[:25]:
        typer.echo(f"    {line}")
    typer.echo()
    pick = _choose(f"Gate '{gate}': what now?", ["Approve", "Reject (stops the run)", "Pause here"], 0)
    if pick == 2:
        raise _Quit()
    if pick == 0:
        eng.decide(gate, True, by=getpass.getuser(), code=rec.code)
    else:
        reason = _answer(typer.prompt(_q("Why?"), default="not right yet"))
        eng.decide(gate, False, by=getpass.getuser(), reason=reason)


def _agent_step(eng: Engine, agent: str, tries: int = 2) -> None:
    """One agent step, headless: the agent writes the step's JSON to outputs/<step>.json; the wizard submits it."""
    node = eng.node()
    out = eng.dir / "outputs" / f"{node.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    error = ""
    for attempt in range(1, tries + 1):
        out.unlink(missing_ok=True)
        brief = eng.instructions().replace(
            f"SUBMIT write JSON then: factory submit {node.name} <file.json>",
            f"SUBMIT write the output JSON to {out} (do NOT run factory submit; the runner submits it)",
        )
        prompt = (
            f"You are running one step of a factory run in {eng.lc.root}. Read the SKILL file, do the step, then "
            f"write the JSON output file and stop.\n\n{brief}"
            + (f"\n\nYOUR LAST OUTPUT WAS REJECTED, fix it:\n{error}" if error else "")
        )
        model = eng.cfg.agents.model_for(node.name, "cursor" if agent != "claude" else "claude")
        cmd = _agent_cmd(agent, prompt, model, eng.lc.state_dir, tools=_STEP_TOOLS)
        log = eng.dir / "logs" / f"{node.name}-{attempt}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        code = _run_watched([cmd], eng.lc.root, [log], lambda: (f"{node.title} ({node.name})", 0, 0), timeout=3600)
        if not out.is_file():
            said = log.read_text(errors="ignore") if log.is_file() else ""
            if re.search(r"Failed to authenticate|not logged in|OAuth|Invalid API key|login", said, re.I):
                login = "claude auth login" if agent == "claude" else f"{agent} login"
                _say(f"  ✗ {agent} is not logged in: run `{login}`, then `factory run {eng.st.ticket}`", hints=True)
                raise _Quit()
            error = f"no output file was written (exit {code}): {' '.join(said.split())[-300:] or 'see ' + str(log)}"
            continue
        try:
            eng.submit(node.name, json.loads(out.read_text()))
            return
        except (FactoryError, ValueError) as e:
            error = str(e)[:2000]
            _say(f"  warning: {node.name} output rejected: {error.splitlines()[0][:150]}", hints=True)
    _say(f"  ✗ {node.title} did not complete after {tries} tries: {error.splitlines()[0][:150]}", hints=True)
    raise _Quit()


def _show(eng: Engine, brief: bool = False) -> None:
    """A person at a terminal gets a short view; an agent (no TTY) gets the full brief for the current step."""
    if not sys.stdout.isatty():
        _say(eng.instructions())
        return
    st, node = eng.st, eng.node()
    names = eng.nodes
    i = next(k for k, n in enumerate(names) if n.name == st.node)
    summary = eng.out("intake").get("summary", "")
    typer.echo()
    typer.echo(f"  {typer.style(st.ticket, bold=True)}  {typer.style(summary[:70], dim=True)}")
    bar = "".join(
        typer.style("━━", fg="green")
        if k < i
        else typer.style("━━", fg="cyan", bold=True)
        if k == i
        else typer.style("━━", dim=True)
        for k in range(len(names))
    )
    typer.echo(f"  {bar}  {typer.style(f'{i + 1}/{len(names)}', dim=True)}")
    typer.echo(f"  {typer.style(st.pipeline, fg='cyan')} workflow · {node.title}")
    typer.echo()
    if brief and not st.finished:  # inside the wizard: the step itself says what happens
        return
    if st.finished:
        ok = st.status == "done"
        mark = typer.style("✓" if ok else "✗", fg="green" if ok else "red", bold=True)
        typer.echo(f"  {mark} {st.outcome}  {st.pr_url or st.stop_reason}")
    elif st.status == "waiting_gate":
        gate = node.gate or ""
        first = eng.gate_summary(gate).splitlines()[0] if eng.gate_summary(gate) else ""
        typer.echo(f"  {typer.style('◆ waiting for you', fg='yellow', bold=True)}  {first[:90]}")
        typer.echo(
            f"  {typer.style('review:', dim=True)} factory gate {gate}    "
            f"{typer.style('then:', dim=True)} factory approve {gate}  |  factory reject {gate} --reason …"
        )
    elif node.kind == "agent":
        typer.echo(f"  {typer.style('▸ next', fg='cyan', bold=True)}  your agent does “{node.title}”")
        typer.echo(f"  {typer.style('in Claude Code:', dim=True)} continue {st.ticket} with the factory")
    else:
        typer.echo(f"  {typer.style('▸ next', fg='cyan', bold=True)}  factory resume")
    if st.workflow_reason and st.workflow_source == "text":
        typer.echo(f"  {typer.style('workflow chosen from the text: ' + st.workflow_reason, dim=True)}")
    typer.echo()


@app.command(name="next")
def next_cmd(run: RunOpt = None) -> None:
    """Show what the agent must do now (or what the run waits on)."""
    _show(_engine(run))


@app.command()
def resume(run: RunOpt = None) -> None:
    """Continue automatic steps of a run (after a restart, a fixed environment or an approval)."""
    eng = _engine(run)
    eng.advance()
    if sys.stdout.isatty() and not os.environ.get("CI"):
        _wizard(eng)
    else:
        _show(eng)


@app.command()
def submit(node: str, file: Path, run: RunOpt = None) -> None:
    """Submit an agent step's JSON output."""
    data = json.loads(file.read_text())
    eng = _engine(run)
    eng.submit(node, data)
    _show(eng)


@app.command()
def schema(node: str) -> None:
    """Print the JSON schema an agent step must submit."""
    step = workflow.find_step(node)
    kind = step.type if step else node
    if kind not in MODELS:
        raise FactoryError(f"no schema for {node}; agent steps: {', '.join(MODELS)}")
    _say(json.dumps(MODELS[kind].model_json_schema(), indent=2))


@app.command()
def status(run: RunOpt = None, all_runs: bool = typer.Option(False, "--all", help="List every run.")) -> None:
    """Progress of the active run, or every run."""
    lc = _lc()
    if all_runs:
        for st in RunStore(lc.runs_dir).all():
            _say(f"{st.id:<34} {st.status:<14} {st.node:<11} {st.outcome or '-':<18} {st.pr_url}")
        return
    eng = Engine.load(lc, run)
    _say(eng.progress())
    if eng.st.stop_reason:
        _say(f"stop: {eng.st.stop_reason}")
    _say(usage.render(_record_usage(eng)), hints=True)


def _record_usage(eng: Engine) -> usage.Usage:
    """Tokens so far for this run, saved on the run so metrics can total them per ticket."""
    u = usage.for_run(eng.st, eng.lc.root)
    eng.st.outputs["_usage"] = u.as_dict()
    eng.store.save(eng.st)
    return u


@app.command()
def tokens(run: RunOpt = None, all_runs: bool = typer.Option(False, "--all", help="Every run, per ticket.")) -> None:
    """Tokens used per ticket: every model call of the Claude Code sessions (and subagents) that drove the run."""
    lc = _lc()
    runs = RunStore(lc.runs_dir).all() if all_runs else [Engine.load(lc, run).st]
    for st in runs:
        u = _record_usage(Engine(lc, st))
        _say(f"\n== {st.ticket}  ({st.id} · {st.status})", hints=True)
        _say(usage.render(u), hints=True)


@app.command()
def risk(run: RunOpt = None) -> None:
    """Explain the run's risk score and autonomy level."""
    eng = _engine(run)
    _say(eng.st.risk.explain() if eng.st.risk else "not assessed yet (after triage)")


@app.command()
def gate(run: RunOpt = None) -> None:
    """Show what the waiting gate asks a human to approve."""
    eng = _engine(run)
    g = eng.node().gate
    if eng.st.status != "waiting_gate" or not g:
        raise FactoryError(f"no gate waiting (run at {eng.st.node}, {eng.st.status})")
    _say(eng.gate_summary(g))


@app.command()
def approve(gate_name: Annotated[str, typer.Argument(metavar="GATE")], run: RunOpt = None) -> None:
    """Approve a waiting gate (human only, interactive)."""
    _human_only("approving a gate")
    eng = _engine(run)
    rec = eng.st.gates.get(gate_name)
    if not rec or rec.decision != "pending":
        raise FactoryError(f"gate {gate_name} is not waiting")
    _say(eng.gate_summary(gate_name))
    typed = typer.prompt(f"\nType {rec.code} to approve gate '{gate_name}'")
    eng.decide(gate_name, True, by=getpass.getuser(), code=typed.strip())
    _show(eng)


@app.command()
def reject(
    gate_name: Annotated[str, typer.Argument(metavar="GATE")],
    reason: str = typer.Option(..., help="Why."),
    run: RunOpt = None,
) -> None:
    """Reject a waiting gate: the run stops."""
    _human_only("rejecting a gate")
    eng = _engine(run)
    eng.decide(gate_name, False, by=getpass.getuser(), reason=reason)
    _show(eng)


@app.command()
def abort(
    reason: str = typer.Option(..., help="Why."),
    rollback: bool = typer.Option(False, help="Restore files changed since the checkpoint."),
    run: RunOpt = None,
) -> None:
    """Stop a run."""
    eng = _engine(run)
    if rollback and eng.st.checkpoint:
        eng.git.save_patch(eng.st.checkpoint, eng.dir / "aborted.patch")
        files = eng.git.rollback(eng.st.checkpoint)
        _say(f"rolled back {len(files)} file(s); patch kept at {eng.dir / 'aborted.patch'}")
    eng.finish("stopped", "aborted", reason)
    eng.save()
    _say(eng.progress())


@app.command(name="events")
def events_cmd(run: RunOpt = None, follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    """Print the event stream (JSONL): the source for metrics, Slack and the office."""
    log = _lc().state_dir / "events.jsonl"
    seen = 0
    while True:
        evs = events.read(log, run)
        for e in evs[seen:]:
            _say(json.dumps(e))
        seen = len(evs)
        if not follow:
            return
        time.sleep(1)


@app.command(name="metrics")
def metrics_cmd(
    since: str = typer.Option("", help="YYYY-MM-DD"), as_json: bool = typer.Option(False, "--json")
) -> None:
    """Aggregate outcomes, gate automation and time to PR across runs."""
    m = metrics.summarize(RunStore(_lc().runs_dir).all(), since)
    _say(json.dumps(m, indent=2) if as_json else metrics.render(m))


# ---------- snapshots & device ----------


def _snap_root(run: str | None) -> Path:
    eng = _engine(run)
    return eng.dir / "snapshots"


@app.command()
def snap(phase: str, label: Annotated[str, typer.Argument()] = "", run: RunOpt = None) -> None:
    """`snap before <label>` / `snap after <label>` capture screen + state; `snap diff` compares them."""
    root = _snap_root(run)
    if phase == "diff":
        for lb, change in snapshot_diff(root):
            _say(f"== {lb}\n    {change}" if change == "unchanged" or "\n" not in change else f"== {lb}\n{change}")
        return
    if not label:
        raise FactoryError("usage: factory snap before|after <label>")
    png, state = make_platform(_lc()).snapshot(root, phase, label)
    _say(f"{png}\n{state}")


@android_app.command("install")
def a_install(run: RunOpt = None) -> None:
    """Build, install and launch the configured variant."""
    eng = _engine(run)
    res = eng.platform.build_install(eng.dir / "logs")
    _say(res.summary)
    raise typer.Exit(0 if res.ok else 1)


@android_app.command("where")
def a_where() -> None:
    """Activity, fragment stack and visible labels."""
    _say(make_platform(_lc()).screen_state())


@android_app.command("tap")
def a_tap(label: str, nth: int = 1) -> None:
    """Tap the element whose text / content-desc matches."""
    _say(make_platform(_lc()).tap(label, nth))


@android_app.command("wait")
def a_wait(text: str, timeout: int = 30) -> None:
    """Poll until text is on screen."""
    ok = make_platform(_lc()).wait_for(text, timeout)
    _say(f"found: {text}" if ok else f"timeout waiting for: {text}")
    raise typer.Exit(0 if ok else 1)


@android_app.command("open")
def a_open(link: str) -> None:
    """Open a deeplink (full URI, or a path under android.deeplink_scheme)."""
    _say(make_platform(_lc()).open_link(link))


@android_app.command("back")
def a_back() -> None:
    """Hardware back."""
    make_platform(_lc()).back()


@android_app.command("launch")
def a_launch() -> None:
    """Launch the app."""
    _say(make_platform(_lc()).launch())


@android_app.command("flow")
def a_flow(flow: Path, run: RunOpt = None) -> None:
    """Run a Maestro flow."""
    eng = _engine(run)
    res = eng.platform.run_flow(flow, eng.dir / "logs")
    _say(res.summary)
    raise typer.Exit(0 if res.ok else 1)


# ---------- guardrail ----------


@guard_app.command("review")
def g_review(
    base: str = typer.Option(..., help="Base branch to diff against."), out: Path = Path(".factory/data/review")
) -> None:
    """Build review context for the current branch outside a run (diff, matched rules, detector findings)."""
    lc = _lc()
    git = Git(lc.root, lc.cfg.project.local_only_paths)
    git.fetch(lc.cfg.vcs.remote, base)
    mb = git.merge_base(f"{lc.cfg.vcs.remote}/{base}")
    ctx = review_ctx.build(lc, git.diff(mb), lc.root / out)
    _say(ctx.summary())
    _say(rl.table(ctx.detector_findings))


@guard_app.command("learn")
def g_learn(
    limit: int = 300,
    bases: str = typer.Option("", help="Comma-separated base branches."),
    owners: str = typer.Option("", help="Comma-separated reviewer logins (default: CODEOWNERS, then top reviewers)."),
    since: str = typer.Option("", help="YYYY-MM-DD, merge into existing data."),
    full: bool = typer.Option(False, "--full", help="Invalidate all harvested data and extract every PR again."),
) -> None:
    """Harvest code-owner review comments from merged PRs into .factory/data/reviews.jsonl (then distill with the guardrail-learn skill)."""
    lc = _lc()
    meta = _harvest(
        lc,
        limit=limit,
        bases=[b for b in bases.split(",") if b] or None,
        owners=[o for o in owners.split(",") if o] or None,
        since=since,
        full=full,
    )
    _say(_harvest_summary(meta), hints=True)
    _say("\n" + harvester.parallel_plan(lc.state_dir / "data"))
    _say(
        "next: ask the agent to distill with the `guardrail-learn` skill into .factory/taste.md, then have the code owner approve it"
    )


@guard_app.command("chunks")
def g_chunks() -> None:
    """Print the parallel tally plan for the harvested reviews (one subagent per line range)."""
    lc = _lc()
    _say(harvester.parallel_plan(lc.state_dir / "data"))


@guard_app.command("check")
def g_check(file: Path) -> None:
    """Validate a findings JSON file and print the verdict table."""
    data: Any = json.loads(file.read_text())
    findings = [rl.Finding.model_validate(f) for f in (data["findings"] if isinstance(data, dict) else data)]
    _say(rl.table(findings))


# ---------- evals ----------


@eval_app.command("add")
def e_add(
    ticket: str, fix_commit: str = typer.Option(..., help="The squashed commit that fixed it."), notes: str = ""
) -> None:
    """Register a past fixed ticket as a golden case."""
    c = Evals(_lc().root).add(ticket, fix_commit, notes)
    _say(f"case {c.id}: base {c.base_commit[:10]}, human changed {len(c.human_files)} file(s)")


@eval_app.command("prepare")
def e_prepare(case: str) -> None:
    """Create a worktree at the commit before the human fix."""
    wt = Evals(_lc().root).prepare(case)
    _say(
        f"worktree: {wt}\nnext: cd {wt} && factory run {case} --base <branch>, then `factory eval score {case} --run <id>` from there"
    )


@eval_app.command("score")
def e_score(case: str, run: RunOpt = None) -> None:
    """Score a finished run against the human fix."""
    eng = _engine(run)
    files = eng.git.changed_files(eng.st.checkpoint) if eng.st.checkpoint else []
    s = Evals(eng.lc.root).score(case, eng.st, files)
    _say(s.model_dump_json(indent=2))


@eval_app.command("report")
def e_report() -> None:
    """Summary across all scored cases."""
    _say(report(Evals(_lc().root).results()))


def main() -> None:
    try:
        app()
    except FactoryError as e:
        typer.echo(f"error: {e}", err=True)
        raise SystemExit(e.exit_code) from None


# ---------- visualiser (works without a factory run, and without factory.yaml) ----------

ToolOpt = Annotated[str, typer.Option("--tool", help="Visualiser backend (default: this repo's, else claude-office).")]


def _viz_here(tool: str) -> tuple[Path, VizConfig]:
    """This repo's visualiser settings, switched on; outside a set-up repo, the defaults."""
    root = config.repo_root()
    try:
        prefs = config.load(root).cfg.viz.model_copy()
    except FactoryError:
        prefs = VizConfig()
    prefs.pixel_agents = True
    if tool:
        prefs.tool = tool
    if prefs.tool not in viz.BACKENDS:
        raise FactoryError(f"unknown visualiser {prefs.tool}; known: {', '.join(viz.BACKENDS)}")
    return root, prefs


@viz_app.command("status")
def viz_status(tool: ToolOpt = "") -> None:
    """Which visualiser, whether it is installed and running, and where to open it."""
    _, prefs = _viz_here(tool)
    v = viz.make(prefs)
    live = v.offices()
    _say(f"== {v.title} ({v.name})", hints=True)
    _say(f"  {'ok' if v.installed() else 'FAIL'}  installed", hints=True)
    _say(f"  {'ok' if live else 'warn'}  running  {live[0].url if live else '`factory viz start`'}", hints=True)
    if v.supports_live_hooks:
        _say(f"  {'ok' if v.live_hooks() else 'warn'}  live hooks", hints=True)


@viz_app.command("start")
def viz_start(tool: ToolOpt = "") -> None:
    """Prepare and start the visualiser (settings, layout, live hooks), then print its link."""
    root, prefs = _viz_here(tool)
    if not _start_office(root, ask=False, prefs=prefs):
        raise typer.Exit(1)


@viz_app.command("stop")
def viz_stop(tool: ToolOpt = "") -> None:
    """Stop every running instance of the visualiser."""
    _, prefs = _viz_here(tool)
    viz.make(prefs).stop()
    _say("  ok  stopped", hints=True)


@viz_app.command("demo")
def viz_demo(
    tool: ToolOpt = "",
    steps: int = typer.Option(3, help="Steps the demo session works through."),
    subagents: int = typer.Option(3, help="Parallel subagents spawned during the middle step (0 = none)."),
    seconds: float = typer.Option(3.0, help="How long each step lasts."),
    sessions: int = typer.Option(
        0, help="Parallel sessions working alongside the main one for the whole demo, like the factory's tallies."
    ),
) -> None:
    """Play a fake session: it arrives, works through steps, fans out subagents, then leaves. No factory needed."""
    root, prefs = _viz_here(tool)
    v = viz.make(prefs)
    if not v.offices():
        raise FactoryError(f"the {v.title} is not running: `factory viz start` first")
    s = viz.Session(v, root, "demo")
    workers = [viz.Session(v, root, f"demo-w{k}") for k in range(1, sessions + 1)]
    _say(f"  watch it: {v.offices()[0].url}", hints=True)
    bar = _Progress()
    s.begin()
    for k, w in enumerate(workers, 1):
        w.begin()
        w.step(f"Parallel work {k}", f"work {k}")
    for n in range(1, steps + 1):
        title = f"Demo step {n}"
        s.step(title, f"step {n}")
        fan = subagents if n == (steps + 1) // 2 else 0
        for k in range(1, fan + 1):
            s.subagent(f"helper {k}")
        for tick in range(int(seconds * 5)):
            bar(title + (f" with {fan} subagents" if fan else ""), tick + 1, int(seconds * 5))
            for w in [s, *workers]:
                w.pulse()
            time.sleep(0.2)
        for _ in range(fan):
            s.subagent_done()
        s.step_done(title)
    bar.close()
    for w in workers:
        w.step_done()
        w.end("done")
    s.end("done")
    _say(f"  ok  demo finished ({1 + len(workers)} sessions)", hints=True)

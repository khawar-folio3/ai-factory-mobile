from __future__ import annotations

import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__, adapters, config, doctor, events, metrics
from . import setup as machine
from .errors import FactoryError, Refused
from .evals import Evals, report
from .gitops import Git
from .guardrail import context as review_ctx
from .guardrail import harvest as harvester
from .guardrail import rules as rl
from .init import init as do_init
from .outputs import MODELS
from .pipeline import Engine
from .platforms import make as make_platform
from .platforms.base import snapshot_diff
from .state import RunStore

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
app.add_typer(eval_app, name="eval")

RunOpt = Annotated[str | None, typer.Option("--run", help="Run id (default: the active run).")]


def _lc() -> config.LoadedConfig:
    return config.load()


def _engine(run: str | None = None) -> Engine:
    return Engine.load(_lc(), run)


def _say(text: str) -> None:
    typer.echo(text)


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
        cfg = config.load().cfg.setup
    except FactoryError:
        cfg = None
    p = machine.plan(machine.tools(cfg), optional=optional)
    _say(p.render())
    if not p.todo:
        _say("machine ready")
        return True
    if os.environ.get("CI") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        _say("\nrun `factory setup` in your own terminal: installs and browser logins need you there")
        return False

    def confirm(s: machine.Step) -> bool:
        return yes or typer.confirm(f"{s.action} {s.tool}: {s.command}", default=True)

    return machine.execute(p, confirm, echo=_say)


@app.command()
def setup(
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without asking per tool."),
    optional: bool = typer.Option(False, help="Also install optional tools (Maestro)."),
) -> None:
    """Install and log in to what the factory needs on this machine: git, gh, twg, JDK, Android tools, Figma."""
    ok = _setup(yes, optional)
    _say("\nnext: `factory doctor` in your project" if ok else "\nfinish the steps above, then `factory setup` again")
    raise typer.Exit(0 if ok else 1)


@app.command()
def init(
    force: bool = typer.Option(False, help="Overwrite an existing factory.yaml."),
    skip_setup: bool = typer.Option(False, help="Do not check or install machine tools first."),
) -> None:
    """Set up this machine (tools + logins), then create factory.yaml and .factory/ for the current repo."""
    if not skip_setup:
        _say("== machine")
        _setup(yes=False, optional=False)
        _say("\n== repo")
    root = Path.cwd()
    for line in do_init(root, force):
        _say(f"  {line}")
    _say(
        f"wrote {root / config.CONFIG_NAME}; review it, then `factory doctor` and `factory install --target claude|cursor`"
    )


@app.command()
def install(target: Annotated[str, typer.Option(help="claude | cursor | all")] = "all") -> None:
    """Install node skills, MCP config and the agent rules block for Claude Code and/or Cursor."""
    lc = _lc()
    targets = ["claude", "cursor"] if target == "all" else [target]
    for t in targets:
        if t not in ("claude", "cursor"):
            raise FactoryError(f"unknown target {t}")
        for p in adapters.install(lc, t):  # type: ignore[arg-type]
            _say(f"  {t}: {p.relative_to(lc.root)}")


@app.command(name="doctor")
def doctor_cmd(offline: bool = typer.Option(False, help="Skip network checks.")) -> None:
    """Check tools, secrets, auth, device and config."""
    text, ok = doctor.render(doctor.checks(_lc(), online=not offline))
    _say(text)
    raise typer.Exit(0 if ok else 1)


@secrets_app.command("set")
def secrets_set(name: str) -> None:
    """Store a secret (typed, never echoed) in the per-machine secrets file."""
    _human_only("setting a secret")
    path = config.secrets_path(_secrets_file())
    value = getpass.getpass(f"{name}: ")
    if not value:
        raise FactoryError("empty value, nothing stored")
    current = config.read_env_file(path)
    current[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in sorted(current.items())))
    os.chmod(path, 0o600)
    _say(f"stored {name} in {path}")


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
    os.execvpe(ctx.args[0], ctx.args, env)  # noqa: S606 - the user's own command


# ---------- runs ----------


@app.command()
def run(
    ticket: str,
    base: Annotated[
        str | None, typer.Option(help="Base branch for this run (required when base_branch is 'ask').")
    ] = None,
    autonomy: Annotated[int | None, typer.Option(min=0, max=4, help="Autonomy ceiling for this run (0-4).")] = None,
) -> None:
    """Start a run for a ticket and advance to the first agent step or gate."""
    eng = Engine.start(_lc(), ticket, ceiling=autonomy, base=base)
    eng.advance()
    _say(eng.instructions())


@app.command(name="next")
def next_cmd(run: RunOpt = None) -> None:
    """Show what the agent must do now (or what the run waits on)."""
    _say(_engine(run).instructions())


@app.command()
def resume(run: RunOpt = None) -> None:
    """Continue automatic steps of a run (after a restart, a fixed environment or an approval)."""
    eng = _engine(run)
    eng.advance()
    _say(eng.instructions())


@app.command()
def submit(node: str, file: Path, run: RunOpt = None) -> None:
    """Submit an agent step's JSON output."""
    data = json.loads(file.read_text())
    eng = _engine(run)
    eng.submit(node, data)
    _say(eng.instructions())


@app.command()
def schema(node: str) -> None:
    """Print the JSON schema an agent step must submit."""
    if node not in MODELS:
        raise FactoryError(f"no schema for {node}; agent steps: {', '.join(MODELS)}")
    _say(json.dumps(MODELS[node].model_json_schema(), indent=2))


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
    _say(eng.instructions())


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
    _say(eng.instructions())


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
    """Print the event stream (JSONL): the source for metrics, Slack and the pixel office."""
    log = _lc().factory_dir / "events.jsonl"
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
) -> None:
    """Harvest code-owner review comments from merged PRs into .factory/data/reviews.jsonl (then distill with the guardrail-learn skill)."""
    lc = _lc()
    repo = Git(lc.root).remote_repo(lc.cfg.vcs.remote)
    meta = harvester.harvest(
        lc.root,
        repo,
        lc.factory_dir / "data",
        limit=limit,
        bases=[b for b in bases.split(",") if b] or None,
        owners=[o for o in owners.split(",") if o] or None,
        since=since,
    )
    _say(json.dumps(meta, indent=2))
    _say(
        "next: ask the agent to distill with the `guardrail-learn` skill into .factory/taste.md, then have the code owner approve it"
    )


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

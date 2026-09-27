"""Workflows as data: each `workflows/<name>.yaml` lists the steps a kind of ticket goes through.

Built-in workflows ship in this package. Developers add or change workflows without touching the repo, in
`~/.config/mobile-factory/workflows/` (every repo) and `<factory home>/workflows/` (one repo); later sources win by
name. A file may `extends: <workflow>` and list `changes` (add/remove/set steps) instead of copying every step.

The engine (pipeline.py) walks any workflow; what a step does comes from its `type` (the step-type registry in
pipeline.py). A workflow declares which of its steps hold the shared artifacts (`artifacts`), so shared steps such as
commit, gates and the PR body never assume bug-fix vocabulary.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from .config import state_dir, state_home
from .errors import ConfigError

ARTIFACTS = ("plan", "before", "change", "after")  # what shared steps read, by role, from a workflow's steps


@dataclass(frozen=True)
class Step:
    name: str
    title: str
    kind: Literal["auto", "agent"]
    type: str
    gate: str | None = None
    task: str = ""
    model: str = ""
    retry_to: str | None = None
    alongside: tuple[str, ...] = ()
    parallel: tuple[str, ...] = ()
    scout: bool = False
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @property
    def skill(self) -> str:
        return str(self.params.get("skill", self.name))

    @property
    def skill_file(self) -> str:
        """A custom skill next to the workflow file that defines this step ('' for built-in skills)."""
        return str(self.params.get("_skill_file", ""))


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    steps: tuple[Step, ...]
    artifacts: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    branch: str = "work/{key}-{slug}"
    pr_template: str = "pr-body.md"
    outcome: str = "pr"  # pr | report | tickets
    max_level: int = 4  # autonomy cap for this kind of work, whatever the repo or run allows
    source: str = "built-in"  # the file it came from
    family: str = ""  # the built-in it descends from (via `extends`); what text detection compares
    raw: dict[str, Any] = field(default_factory=dict, hash=False, compare=False, repr=False)  # as written, for extends

    def step(self, name: str) -> Step:
        for s in self.steps:
            if s.name == name:
                return s
        raise ConfigError(f"workflow {self.name} has no step {name}")

    def artifact(self, role: str) -> str:
        """The step whose output plays `role` (plan, before, change, after); '' when the workflow has none."""
        return self.artifacts.get(role, "")

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.steps]


def _step(d: dict[str, Any]) -> Step:
    known = set(STEP_KEYS)
    return Step(
        name=d["name"],
        title=d.get("title", d["name"].replace("_", " ").capitalize()),
        kind=d.get("kind", "auto"),
        type=d.get("type", d["name"]),
        gate=d.get("gate"),
        task=" ".join(str(d.get("task", "")).split()),
        model=d.get("model", ""),
        retry_to=d.get("retry_to"),
        alongside=tuple(d.get("alongside", ())),
        parallel=tuple(d.get("parallel", ())),
        scout=bool(d.get("scout", False)),
        params={k: v for k, v in d.items() if k not in known},
    )


STEP_KEYS = ("name", "title", "kind", "type", "gate", "task", "model", "retry_to", "alongside", "parallel", "scout")
WORKFLOW_KEYS = ("description", "branch", "pr_template", "outcome", "max_level", "artifacts")


def _apply(base: dict[str, Any], changes: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    """`extends` edits: {add: {...}, after|before: step} · {remove: step} · {set: step, <key>: value, ...}."""
    steps: list[dict[str, Any]] = copy.deepcopy(base["steps"])

    def at(name: str) -> int:
        for i, st in enumerate(steps):
            if st["name"] == name:
                return i
        raise ConfigError(f"{where}: no step {name} to change")

    for ch in changes:
        if "add" in ch:
            new = ch["add"]
            anchor = ch.get("after") or ch.get("before")
            if not anchor:
                raise ConfigError(f"{where}: `add` needs `after:` or `before:` a step")
            i = at(anchor) + (1 if "after" in ch else 0)
            steps.insert(i, new)
        elif "remove" in ch:
            steps.pop(at(ch["remove"]))
        elif "set" in ch:
            steps[at(ch["set"])].update({k: v for k, v in ch.items() if k != "set"})
        else:
            raise ConfigError(f"{where}: a change is add/remove/set, got {sorted(ch)}")
    return steps


def _parse(name: str, text: str, known: dict[str, Workflow] | None = None, source: str = "built-in") -> Workflow:
    d = yaml.safe_load(text) or {}
    where = f"workflow {name} ({source})"
    if parent := d.get("extends"):
        base = (known or {}).get(parent)
        if not base:
            raise ConfigError(f"{where}: extends unknown workflow {parent}")
        raw = {**base.raw, **{k: v for k, v in d.items() if k in WORKFLOW_KEYS}}
        raw["steps"] = d["steps"] if "steps" in d else _apply(base.raw, d.get("changes") or [], where)
        raw["_family"] = base.family
        d = raw
    if "steps" not in d:
        raise ConfigError(f"{where}: needs `steps` (or `extends` + `changes`)")
    folder = Path(source).parent if source != "built-in" else None
    steps = []
    for sd in d["steps"]:
        st = _step(sd)
        if folder and (custom := folder / f"{st.skill}.md").is_file():
            st.params["_skill_file"] = str(custom)
        steps.append(st)
    wf = Workflow(
        name=name,
        description=d.get("description", ""),
        steps=tuple(steps),
        artifacts=dict(d.get("artifacts", {})),
        branch=d.get("branch", "work/{key}-{slug}"),
        pr_template=d.get("pr_template", "pr-body.md"),
        outcome=d.get("outcome", "pr"),
        max_level=int(d.get("max_level", 4)),
        source=source,
        family=str(d.get("_family") or (name if source == "built-in" else "")),
        raw=d,
    )
    names = wf.names
    if len(set(names)) != len(names):
        raise ConfigError(f"{where}: step names repeat: {names}")
    for s in wf.steps:
        if s.retry_to and s.retry_to not in names:
            raise ConfigError(f"{where}: step {s.name} retries to unknown step {s.retry_to}")
        if s.kind == "agent" and not s.task:
            raise ConfigError(f"{where}: agent step {s.name} needs a task")
        if s.kind not in ("auto", "agent"):
            raise ConfigError(f"{where}: step {s.name} kind must be auto or agent")
    for role, step in wf.artifacts.items():
        if role not in ARTIFACTS or step not in names:
            raise ConfigError(f"{where}: artifact {role} -> {step} is not a known role/step")
    return wf


def folders(root: Path | None = None) -> list[Path]:
    """Where developers keep their own workflows, lowest priority first."""
    out = [state_home() / "workflows"]
    if root is not None:
        out.append(state_dir(root) / "workflows")
    return out


def all_workflows(root: Path | None = None) -> dict[str, Workflow]:
    found: dict[str, Workflow] = {}
    pkg = resources.files("mobile_factory.workflows")
    for p in sorted(pkg.iterdir(), key=lambda p: p.name):
        if p.name.endswith(".yaml"):
            found[p.name[:-5]] = _parse(p.name[:-5], p.read_text(), found)
    for folder in folders(root):
        files = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
        # a file may extend another custom one: parse bases (no `extends`) first
        for f in sorted(files, key=lambda f: "extends:" in f.read_text()):
            found[f.stem] = _parse(f.stem, f.read_text(), found, str(f))
    return found


def get(name: str, root: Path | None = None) -> Workflow:
    wf = all_workflows(root).get(name)
    if not wf:
        raise ConfigError(f"no workflow {name}; known: {', '.join(all_workflows(root))}")
    return wf


def find_step(name: str, root: Path | None = None) -> Step | None:
    """A step by name in any workflow (for `factory schema <step>`)."""
    return next((s for wf in all_workflows(root).values() for s in wf.steps if s.name == name), None)

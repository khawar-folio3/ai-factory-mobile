"""Workflows as data: each `workflows/<name>.yaml` lists the steps a kind of ticket goes through.

The engine (pipeline.py) walks any workflow; what a step does comes from its `type` (the step-type registry in
pipeline.py). A workflow declares which of its steps hold the shared artifacts (`artifacts`), so shared steps such as
commit, gates and the PR body never assume bug-fix vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any, Literal

import yaml

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
    known = {"name", "title", "kind", "type", "gate", "task", "model", "retry_to", "alongside", "parallel", "scout"}
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


def _parse(name: str, text: str) -> Workflow:
    d = yaml.safe_load(text)
    wf = Workflow(
        name=name,
        description=d.get("description", ""),
        steps=tuple(_step(s) for s in d["steps"]),
        artifacts=dict(d.get("artifacts", {})),
        branch=d.get("branch", "work/{key}-{slug}"),
        pr_template=d.get("pr_template", "pr-body.md"),
        outcome=d.get("outcome", "pr"),
        max_level=int(d.get("max_level", 4)),
    )
    names = wf.names
    for s in wf.steps:
        if s.retry_to and s.retry_to not in names:
            raise ConfigError(f"workflow {name}: step {s.name} retries to unknown step {s.retry_to}")
        if s.kind == "agent" and not s.task:
            raise ConfigError(f"workflow {name}: agent step {s.name} needs a task")
    for role, step in wf.artifacts.items():
        if role not in ARTIFACTS or step not in names:
            raise ConfigError(f"workflow {name}: artifact {role} -> {step} is not a known role/step")
    return wf


@cache
def all_workflows() -> dict[str, Workflow]:
    root = resources.files("mobile_factory.workflows")
    return {
        p.name.removesuffix(".yaml"): _parse(p.name.removesuffix(".yaml"), p.read_text())
        for p in sorted(root.iterdir(), key=lambda p: p.name)
        if p.name.endswith(".yaml")
    }


def get(name: str) -> Workflow:
    wf = all_workflows().get(name)
    if not wf:
        raise ConfigError(f"no workflow {name}; known: {', '.join(all_workflows())}")
    return wf


def find_step(name: str) -> Step | None:
    """A step by name in any workflow (for `factory schema <step>`)."""
    return next((s for wf in all_workflows().values() for s in wf.steps if s.name == name), None)

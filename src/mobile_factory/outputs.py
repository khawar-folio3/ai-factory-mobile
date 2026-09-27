from __future__ import annotations

import re
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


EXAMPLES: dict[str, dict[str, object]] = {
    "research": {
        "question": "Can we drop the legacy map SDK?",
        "answer": "Yes after OMX moves to MapUIKit; two screens still use it.",
        "findings": ["library/.../LegacyMapFragment.kt:40 is the last user", "PR #1812 migrated floors"],
        "recommendation": "Migrate the two screens, then remove the dependency",
    },
    "split": {
        "summary": "Room booking v2",
        "stories": [{"summary": "Show room capacity on the room card", "acceptance_criteria": ["Capacity shows"]}],
    },
    "spec": {
        "summary": "Visitor check-in app for front desks",
        "screens": ["home", "check_in"],
        "acceptance_criteria": ["Home lists today's visitors", "Tapping a visitor checks them in"],
        "stories": [{"summary": "Print a visitor badge", "acceptance_criteria": ["A badge prints on check-in"]}],
    },
    "architecture": {
        "summary": "Single-activity Compose app",
        "platform": "android-kotlin-compose",
        "modules": ["app", "core:data", "feature:checkin"],
        "decisions": [{"topic": "DI", "choice": "Hilt", "why": "team standard"}],
    },
    "custom": {"summary": "Checked the analytics events", "files": ["context/analytics.md"], "ok": True},
    "work": {
        "summary": "Wrap avatar container height on profile header",
        "acceptance_criteria": [
            {"criterion": "The full avatar shows on Profile", "met": True, "evidence": "criterion 1 passed"},
        ],
        "flow": "<run>/flows/work.yaml",
        "notes": "",
    },
}


class Criterion(BaseModel):
    criterion: str
    met: bool
    evidence: str = Field(min_length=3, description="snapshot label, test name or what was observed")
    blocked: bool = Field(False, description="cannot be checked for an external reason (backend data, access)")
    reason: str = Field("", description="why it is blocked (required when blocked)")

    @model_validator(mode="after")
    def _reason_when_blocked(self) -> Criterion:
        if self.blocked and not self.reason.strip():
            raise ValueError(f"'{self.criterion}' is blocked: say why in `reason`")
        return self


class WorkOut(_Out):
    summary: str = Field("", max_length=72, description="imperative commit subject, without the ticket key")
    acceptance_criteria: list[Criterion] = Field(default_factory=list, description="each met or blocked, with evidence")
    flow: str = Field("", description="the Maestro flow that checks every criterion")
    notes: str = ""
    questions: list[str] = Field(default_factory=list, description="the ticket is unclear: ask these, change nothing")
    stop: str = Field("", description="too big or not a code change: why the run stops")


class Option(BaseModel):
    name: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)


class ResearchOut(_Out):
    question: str = Field(min_length=5)
    answer: str = Field(min_length=10, description="the short answer, first")
    findings: list[str] = Field(min_length=1, description="facts with evidence: path:line, commit, PR, doc link")
    options: list[Option] = Field(default_factory=list)
    recommendation: str = ""
    open_questions: list[str] = Field(default_factory=list)


class Story(BaseModel):
    summary: str = Field(min_length=5, max_length=120)
    description: str = ""
    acceptance_criteria: list[str] = Field(min_length=1)
    type: str = "Story"


class SplitOut(_Out):
    summary: str = Field(min_length=3, description="one line: what the epic delivers")
    stories: list[Story] = Field(min_length=1)
    rationale: str = ""


class SpecOut(_Out):
    verdict: Literal["eligible", "needs-info"] = "eligible"
    summary: str = Field(min_length=3, description="one line: the app and who it is for")
    users: list[str] = Field(default_factory=list)
    screens: list[str] = Field(default_factory=list, description="labels of the first slice's screens")
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="for the FIRST slice only: observable on the device or in a test"
    )
    stories: list[Story] = Field(default_factory=list, description="the remaining slices, each a future story")
    non_functional: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    reason: str = ""


class Decision(BaseModel):
    topic: str
    choice: str
    why: str = ""


class ArchitectureOut(_Out):
    summary: str = Field(min_length=3)
    platform: str = Field(min_length=2, description="e.g. android-kotlin-compose")
    modules: list[str] = Field(min_length=1)
    decisions: list[Decision] = Field(min_length=1)
    libraries: list[str] = Field(default_factory=list, description="name:version")


class CustomOut(_Out):
    """A custom agent step's output: what it did and where its results are."""

    summary: str = Field(min_length=3)
    details: str = ""
    files: list[str] = Field(default_factory=list, description="files it wrote, e.g. in context/")
    ok: bool = Field(True, description="false stops the run (or retries, when the step has retry_to)")


MODELS: dict[str, type[_Out]] = {
    "research": ResearchOut,
    "split": SplitOut,
    "spec": SpecOut,
    "architecture": ArchitectureOut,
    "custom": CustomOut,
    "work": WorkOut,
}


def fields(model: type[BaseModel]) -> str:
    """What an output needs, one line: `name*: type` (* = required), nested models spelled out after it."""
    nested: dict[str, type[BaseModel]] = {}

    def name(a: object) -> str:
        for x in (a, *get_args(a)):
            if isinstance(x, type) and issubclass(x, BaseModel):
                nested[x.__name__] = x
        return a.__name__ if isinstance(a, type) else re.sub(r"\b(?:\w+\.)+(\w+)", r"\1", str(a))

    line = ", ".join(
        f"{n}{'*' if f.is_required() else ''}: {name(f.annotation)}" for n, f in model.model_fields.items()
    )
    return line + "".join(f"; {k} = {{{fields(m)}}}" for k, m in nested.items())

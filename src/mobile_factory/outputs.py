from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .guardrail.rules import Finding


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriageOut(_Out):
    verdict: Literal["eligible", "needs-info", "ineligible", "duplicate", "already-fixed"]
    reason: str = Field(min_length=3)
    summary: str = Field(min_length=3, description="one line: what is wrong, where")
    category: str = Field("", description="e.g. crash, ui_spacing, copy, navigation, state, network, color_token")
    root_cause_hypothesis: str = ""
    plan: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list, description="files/packages expected to change")
    estimated_files: int = Field(0, ge=0)
    public_api_change: bool = False
    risk_classes: list[str] = Field(
        default_factory=list, description="payment, auth, security, data_loss, migration, ..."
    )
    questions: list[str] = Field(default_factory=list, description="for needs-info: what the reporter must answer")


class ReproOut(_Out):
    reproduced: bool
    confidence: float = Field(ge=0, le=1)
    steps: list[str] = Field(min_length=1)
    snapshots: list[str] = Field(default_factory=list, description="labels captured with `factory snap before <label>`")
    notes: str = ""


class FixOut(_Out):
    summary: str = Field(min_length=5, max_length=72, description="imperative commit subject, without the ticket key")
    root_cause: str = Field(min_length=5)
    changes: str = Field(min_length=5, description="what changed and why this is the smallest fix")
    tests_added: bool = False
    notes: str = ""


class VerifyOut(_Out):
    defect_fixed: bool
    adjacent_unchanged: bool
    snapshots: list[str] = Field(default_factory=list, description="labels captured with `factory snap after <label>`")
    notes: str = ""


class ReviewOut(_Out):
    findings: list[Finding] = Field(default_factory=list)


EXAMPLES: dict[str, dict[str, object]] = {
    "triage": {
        "verdict": "eligible",
        "reason": "clear repro steps, single screen, no backend change",
        "summary": "Profile avatar is clipped on small screens",
        "category": "ui_spacing",
        "root_cause_hypothesis": "fixed 48dp height in ProfileHeader",
        "plan": ["make the avatar container wrap_content", "add a screenshot label for small devices"],
        "areas": ["feature/profile/src/main/java/.../ProfileHeader.kt"],
        "estimated_files": 1,
        "public_api_change": False,
        "risk_classes": [],
    },
    "reproduce": {
        "reproduced": True,
        "confidence": 0.9,
        "steps": ["open Profile", "observe avatar"],
        "snapshots": ["profile"],
    },
    "fix": {
        "summary": "Wrap avatar container height on profile header",
        "root_cause": "hardcoded height clipped the image",
        "changes": "ProfileHeader.kt: height -> wrapContentHeight(); unit test unchanged (layout only)",
        "tests_added": False,
    },
    "verify": {"defect_fixed": True, "adjacent_unchanged": True, "snapshots": ["profile", "settings"]},
    "review": {
        "findings": [
            {
                "file": "feature/profile/ProfileHeader.kt",
                "line": 42,
                "rule": "S001",
                "severity": "major",
                "suggestion": "remove narrating comment",
                "outcome": "applied",
            }
        ]
    },
    "plan": {
        "verdict": "eligible",
        "reason": "one screen, existing API, fits one PR",
        "summary": "Show a Favourites filter on the Spaces list",
        "acceptance_criteria": ["A Favourites chip appears above the list", "Tapping it shows only favourited spaces"],
        "plan": ["add chip to SpacesFilterBar", "filter in SpacesViewModel", "unit test the filter"],
        "areas": ["feature/spaces"],
        "screens": ["spaces_list"],
        "estimated_files": 3,
    },
    "baseline": {"snapshots": ["spaces_list"], "steps": ["open Spaces tab"]},
    "implement": {
        "summary": "Add a Favourites filter to the Spaces list",
        "changes": "chip + VM filter",
        "tests_added": True,
    },
    "accept": {
        "criteria": [
            {"criterion": "A Favourites chip appears above the list", "met": True, "evidence": "snap spaces_list"},
            {"criterion": "Tapping it shows only favourited spaces", "met": True, "evidence": "SpacesViewModelTest"},
        ],
        "snapshots": ["spaces_list"],
    },
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
    "scaffold": {"summary": "Create the visitor app with the check-in slice", "changes": "project + slice"},
}


class PlanOut(_Out):
    verdict: Literal["eligible", "needs-info", "ineligible", "too-big", "duplicate"]
    reason: str = Field(min_length=3)
    summary: str = Field(min_length=3, description="one line: what the user gets")
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="each one observable on the device or in a test; from the ticket or drafted"
    )
    plan: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list, description="files/packages expected to change")
    screens: list[str] = Field(default_factory=list, description="labels of the screens to baseline and check")
    designs: list[str] = Field(default_factory=list, description="Figma links or frame names the change follows")
    estimated_files: int = Field(0, ge=0)
    public_api_change: bool = False
    risk_classes: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, description="for needs-info: what the reporter must answer")
    subtasks: list[str] = Field(default_factory=list, description="for too-big: one line per proposed sub-task")


class BaselineOut(_Out):
    snapshots: list[str] = Field(min_length=1, description="labels captured with `factory snap before <label>`")
    steps: list[str] = Field(default_factory=list, description="how to reach each screen")
    notes: str = ""


class ImplementOut(_Out):
    summary: str = Field(min_length=5, max_length=72, description="imperative commit subject, without the ticket key")
    changes: str = Field(min_length=5, description="what changed and why, briefly")
    tests_added: bool = False
    notes: str = ""


class Criterion(BaseModel):
    criterion: str
    met: bool
    evidence: str = Field(min_length=3, description="snapshot label, test name or what was observed")


class AcceptOut(_Out):
    criteria: list[Criterion] = Field(min_length=1)
    snapshots: list[str] = Field(default_factory=list, description="labels captured with `factory snap after <label>`")
    notes: str = ""


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


MODELS: dict[str, type[_Out]] = {
    "triage": TriageOut,
    "reproduce": ReproOut,
    "fix": FixOut,
    "verify": VerifyOut,
    "review": ReviewOut,
    "plan": PlanOut,
    "baseline": BaselineOut,
    "implement": ImplementOut,
    "accept": AcceptOut,
    "research": ResearchOut,
    "split": SplitOut,
    "spec": SpecOut,
    "architecture": ArchitectureOut,
    "scaffold": ImplementOut,
}

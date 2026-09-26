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
}

MODELS: dict[str, type[_Out]] = {
    "triage": TriageOut,
    "reproduce": ReproOut,
    "fix": FixOut,
    "verify": VerifyOut,
    "review": ReviewOut,
}

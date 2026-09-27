from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .autonomy import RiskAssessment, RiskSignals
from .errors import FactoryError

Status = Literal["running", "waiting_agent", "waiting_gate", "waiting_answers", "done", "stopped"]


class GateRecord(BaseModel):
    gate: str
    decision: Literal["pending", "auto", "approved", "rejected"] = "pending"
    code: str = ""
    by: str = ""
    at: str = ""
    reason: str = ""
    sha: str = ""


class RunState(BaseModel):
    id: str
    ticket: str
    pipeline: str = "light"  # the workflow
    workflow_source: str = ""  # jira | text | override
    kind: str = ""  # a code ticket: bugfix | task | feature (names the branch)
    workflow_reason: str = ""
    created_at: str
    updated_at: str
    status: Status = "running"
    node: str
    ceiling: int
    base: str = ""
    branch: str = ""
    checkpoint: str = ""
    fix_attempts: int = 0
    question_rounds: int = 0
    signals: RiskSignals = Field(default_factory=RiskSignals)
    risk: RiskAssessment | None = None
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    gates: dict[str, GateRecord] = Field(default_factory=dict)
    outcome: str = ""
    stop_reason: str = ""
    pr_url: str = ""
    history: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)  # optional steps turned on for this run (`factory run --tests`)
    started: dict[str, float] = Field(default_factory=dict)  # step -> epoch its current attempt began

    @property
    def level(self) -> int:
        return self.risk.level if self.risk else 0

    @property
    def finished(self) -> bool:
        return self.status in ("done", "stopped")

    def duration_sec(self) -> int:
        a = datetime.fromisoformat(self.created_at)
        b = datetime.fromisoformat(self.updated_at)
        return int((b - a).total_seconds())


def new_run_id(ticket: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{re.sub(r'[^A-Za-z0-9-]', '-', ticket)}-{stamp}"


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.dir = runs_dir

    def path(self, run_id: str) -> Path:
        return self.dir / run_id

    def save(self, st: RunState) -> None:
        st.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        d = self.path(st.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "state.json.tmp"
        tmp.write_text(st.model_dump_json(indent=2))
        os.replace(tmp, d / "state.json")
        (self.dir / "CURRENT").write_text(st.id)

    def load(self, run_id: str | None = None) -> RunState:
        rid = run_id or self.current()
        f = self.path(rid) / "state.json"
        if not f.is_file():
            raise FactoryError(f"no run {rid}")
        return RunState.model_validate(json.loads(f.read_text()))

    def current(self) -> str:
        cur = self.dir / "CURRENT"
        if not cur.is_file():
            raise FactoryError("no active run: start one with `factory run <TICKET>`")
        return cur.read_text().strip()

    def all(self) -> list[RunState]:
        if not self.dir.is_dir():
            return []
        return [
            RunState.model_validate(json.loads((d / "state.json").read_text()))
            for d in sorted(self.dir.iterdir())
            if (d / "state.json").is_file()
        ]

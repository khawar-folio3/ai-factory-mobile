from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .config import state_dir
from .errors import FactoryError
from .gitops import Git
from .state import RunState


class EvalCase(BaseModel):
    id: str
    ticket: str
    fix_commit: str
    base_commit: str
    human_files: list[str]
    notes: str = ""


class EvalScore(BaseModel):
    case: str
    run: str
    outcome: str
    reached_pr: bool
    file_precision: float
    file_recall: float
    fix_attempts: int
    review_rounds: int
    human_gates: int
    risk: int | None
    level: int
    scored_at: str


class Evals:
    def __init__(self, root: Path) -> None:
        self.git = Git(root)
        # cases and results live in the main checkout, also when called from an eval worktree
        self.root = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")).parent
        self.dir = state_dir(self.root) / "evals"

    def case_file(self, case_id: str) -> Path:
        return self.dir / "cases" / f"{case_id}.yaml"

    def add(self, ticket: str, fix_commit: str, notes: str = "") -> EvalCase:
        sha = self.git("rev-parse", fix_commit)
        parents = self.git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        if len(parents) != 1:
            raise FactoryError(f"{fix_commit} has {len(parents)} parents: pick the squashed fix commit, not a merge")
        files = self.git("diff", "--name-only", parents[0], sha).splitlines()
        case = EvalCase(
            id=ticket, ticket=ticket, fix_commit=sha, base_commit=parents[0], human_files=files, notes=notes
        )
        f = self.case_file(case.id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump(case.model_dump(), sort_keys=False))
        return case

    def load(self, case_id: str) -> EvalCase:
        f = self.case_file(case_id)
        if not f.is_file():
            raise FactoryError(f"no eval case {case_id}")
        return EvalCase.model_validate(yaml.safe_load(f.read_text()))

    def cases(self) -> list[EvalCase]:
        d = self.dir / "cases"
        return (
            [EvalCase.model_validate(yaml.safe_load(f.read_text())) for f in sorted(d.glob("*.yaml"))]
            if d.is_dir()
            else []
        )

    def prepare(self, case_id: str) -> Path:
        c = self.load(case_id)
        wt = self.dir / "work" / case_id
        if wt.exists():
            raise FactoryError(f"{wt} exists: `git worktree remove {wt}` first")
        self.git("worktree", "add", "--detach", str(wt), c.base_commit)
        # nothing to copy: the worktree shares this repo's factory home (config, taste, tickets)
        return wt

    def score(self, case_id: str, st: RunState, run_files: list[str]) -> EvalScore:
        c = self.load(case_id)
        human, ours = set(c.human_files), set(run_files)
        hit = len(human & ours)
        s = EvalScore(
            case=case_id,
            run=st.id,
            outcome=st.outcome or st.status,
            reached_pr=st.node in ("publish", "handoff") or st.outcome in ("draft-pr", "pr"),
            file_precision=round(hit / len(ours), 2) if ours else 0.0,
            file_recall=round(hit / len(human), 2) if human else 0.0,
            fix_attempts=st.fix_attempts,
            review_rounds=st.review_rounds,
            human_gates=sum(1 for g in st.gates.values() if g.decision in ("approved", "rejected")),
            risk=st.risk.score if st.risk else None,
            level=st.level,
            scored_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "results.jsonl").open("a") as f:
            f.write(s.model_dump_json() + "\n")
        return s

    def results(self) -> list[dict[str, Any]]:
        f = self.dir / "results.jsonl"
        return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()] if f.is_file() else []


def report(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no eval results yet: factory eval add -> prepare -> run in the worktree -> factory eval score"
    latest = {r["case"]: r for r in rows}
    n = len(latest)
    pr = sum(1 for r in latest.values() if r["reached_pr"])
    prec = sum(r["file_precision"] for r in latest.values()) / n
    rec = sum(r["file_recall"] for r in latest.values()) / n
    lines = [f"cases {n} · reached PR {pr}/{n} · file precision {prec:.2f} · file recall {rec:.2f}"]
    lines += [
        f"  {r['case']:<16} {r['outcome']:<18} P {r['file_precision']:.2f} R {r['file_recall']:.2f} "
        f"attempts {r['fix_attempts']} gates {r['human_gates']} risk {r['risk']} L{r['level']}"
        for r in latest.values()
    ]
    return "\n".join(lines)

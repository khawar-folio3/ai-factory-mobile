from __future__ import annotations

from pydantic import BaseModel, Field

from .config import AutonomyConfig, GateConfig

LEVELS = {
    0: "manual: every gate asks a human",
    1: "assisted: the spec is automatic; the rest ask (a code ticket: only the PR asks)",
    2: "supervised: the spec is automatic; the PR asks",
    3: "trusted: only the PR preview asks",
    4: "autonomous: runs to a draft PR without asking",
}


class RiskSignals(BaseModel):
    category: str = ""
    estimated_files: int = 0
    actual_files: int | None = None
    modules_touched: int = 0
    lines_changed: int = 0
    public_api_change: bool = False
    risk_classes: list[str] = Field(default_factory=list)
    tests_added: bool | None = None
    platforms: int = 1


class RiskAssessment(BaseModel):
    score: int
    allowed_level: int
    ceiling: int
    level: int
    factors: list[tuple[str, int]]

    def explain(self) -> str:
        rows = [f"  {pts:+4d}  {why}" for why, pts in self.factors] or ["     0  no risk signals"]
        return "\n".join(
            [
                f"risk {self.score}/100 -> allows L{self.allowed_level}, ceiling L{self.ceiling} "
                f"=> autonomy L{self.level} ({LEVELS[self.level]})",
                *rows,
            ]
        )


def score(s: RiskSignals, cfg: AutonomyConfig) -> list[tuple[str, int]]:
    f: list[tuple[str, int]] = []
    files = s.actual_files if s.actual_files is not None else s.estimated_files
    if files:
        f.append((f"{files} file(s) {'changed' if s.actual_files is not None else 'expected'}", min(files, 10) * 3))
    if s.modules_touched > 1:
        f.append((f"{s.modules_touched} modules", min((s.modules_touched - 1) * 10, 20)))
    if s.lines_changed > 200:
        f.append((f"{s.lines_changed} lines changed", 15))
    elif s.lines_changed > 50:
        f.append((f"{s.lines_changed} lines changed", 5))
    if s.public_api_change:
        f.append(("public API / contract change", 20))
    high = sorted(set(s.risk_classes) & set(cfg.high_risk_classes))
    if high:
        f.append((f"high-risk area: {', '.join(high)}", 40))
    if s.platforms > 1:
        f.append((f"{s.platforms} platforms", 15))
    if s.category in cfg.low_risk_categories:
        f.append((f"low-risk category: {s.category}", -10))
    return f


def assess(s: RiskSignals, cfg: AutonomyConfig, ceiling: int | None = None) -> RiskAssessment:
    factors = score(s, cfg)
    total = max(0, min(100, sum(p for _, p in factors)))
    allowed = max((lvl for lvl, limit in cfg.thresholds.items() if total <= limit), default=0)
    ceil = cfg.ceiling if ceiling is None else ceiling
    return RiskAssessment(score=total, allowed_level=allowed, ceiling=ceil, level=min(allowed, ceil), factors=factors)


def gate_is_automatic(gate: GateConfig, level: int) -> bool:
    return level >= gate.auto_at

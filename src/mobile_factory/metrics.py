from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any

from .state import RunState


def summarize(runs: list[RunState], since: str = "") -> dict[str, Any]:
    runs = [r for r in runs if r.created_at[:10] >= since]
    finished = [r for r in runs if r.finished]
    gates = [g for r in runs for g in r.gates.values() if g.decision != "pending"]
    prs = [r for r in finished if r.outcome in ("draft-pr", "pr")]
    return {
        "runs": len(runs),
        "finished": len(finished),
        "outcomes": dict(Counter(r.outcome for r in finished).most_common()),
        "pr_rate": round(len(prs) / len(finished), 2) if finished else 0.0,
        "median_minutes_to_pr": round(median(r.duration_sec() for r in prs) / 60, 1) if prs else None,
        "median_fix_attempts": median(r.fix_attempts for r in finished) if finished else None,
        "gates_total": len(gates),
        "gates_auto": sum(1 for g in gates if g.decision == "auto"),
        "gates_human": sum(1 for g in gates if g.decision in ("approved", "rejected")),
        "gates_rejected": sum(1 for g in gates if g.decision == "rejected"),
        "levels": dict(Counter(f"L{r.level}" for r in runs if r.risk).most_common()),
        "median_risk": median(r.risk.score for r in runs if r.risk) if any(r.risk for r in runs) else None,
        "median_tokens_per_ticket": median(t) if (t := _tokens(finished)) else None,
        "tokens_total": sum(_tokens(runs)),
    }


def _tokens(runs: list[RunState]) -> list[int]:
    return [int(r.outputs["_usage"]["total"]) for r in runs if r.outputs.get("_usage", {}).get("total")]


def render(m: dict[str, Any]) -> str:
    auto = f"{m['gates_auto']}/{m['gates_total']}" if m["gates_total"] else "0/0"
    lines = [
        f"runs {m['runs']} · finished {m['finished']} · PR rate {m['pr_rate']:.0%} · median time to PR {m['median_minutes_to_pr']} min",
        f"gates: auto {auto} · human {m['gates_human']} (rejected {m['gates_rejected']}) · median fix attempts {m['median_fix_attempts']}",
        f"autonomy levels {m['levels']} · median risk {m['median_risk']}",
        "outcomes: " + ", ".join(f"{k} {v}" for k, v in m["outcomes"].items()),
        f"tokens: {m['tokens_total']:,} total · median per ticket {m['median_tokens_per_ticket'] or '-'}"
        " (`factory tokens --all` per ticket)",
    ]
    return "\n".join(lines)

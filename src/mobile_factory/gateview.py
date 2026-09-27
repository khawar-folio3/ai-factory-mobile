from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from . import autonomy
from .config import GateConfig

if TYPE_CHECKING:
    from .pipeline import Engine

KEY = 12  # key column width
BAR = 20
_TITLES = {
    "plan": "Approve the spec",
    "pr": "Open the draft PR",
}


def _dim(text: str) -> str:
    return typer.style(text, dim=True)


def _key(name: str, value: str = "") -> str:
    return f"  {typer.style(f'{name:<{KEY}}', fg='cyan', bold=True)}{value}"


def _more(text: str) -> str:
    return " " * (2 + KEY) + text


def _bar(value: float, color: str) -> str:
    fill = max(1 if value > 0 else 0, min(BAR, round(value * BAR)))
    return typer.style("█" * fill, fg=color) + _dim("░" * (BAR - fill))


def header(eng: Engine, gate: str) -> str:
    title = _TITLES.get(gate, f"Gate '{gate}'")
    line = typer.style(f"▌ {title}", fg="yellow", bold=True) + _dim(f"   {eng.st.ticket} · gate '{gate}'")
    return f"{line}\n{_dim('─' * 64)}"


def risk_block(eng: Engine) -> list[str]:
    r = eng.st.risk
    if not r:
        return []
    color, word = ("green", "low") if r.score <= 35 else ("yellow", "medium") if r.score <= 55 else ("red", "high")
    out = [
        _key(
            "Risk",
            f"{_bar(r.score / 100, color)}  {typer.style(f'{r.score}/100', bold=True)}  {typer.style(word, fg=color)}",
        )
    ]
    out += [_more(_dim(f"{pts:+4d}  {why}")) for why, pts in r.factors]
    name = autonomy.LEVELS[r.level].split(":")[0]
    if r.level < r.allowed_level:
        why = f"risk allows L{r.allowed_level}, capped by your ceiling L{r.ceiling}"
    elif r.level < r.ceiling:
        why = f"lowered from your ceiling L{r.ceiling} by the risk"
    else:
        why = ""
    out.append("")
    out.append(
        _key(
            "Autonomy",
            f"{typer.style(f'L{r.level} {name}', bold=True)}" + (f"  {_dim('(' + why + ')')}" if why else ""),
        )
    )
    gates = [n.gate for n in eng.nodes if n.gate]
    asks = [g for g in gates if not autonomy.gate_is_automatic(eng.cfg.gates.get(g, GateConfig(auto_at=5)), r.level)]
    if asks:
        out.append(_more(_dim("asks you at: ") + " · ".join(typer.style(g, bold=g == eng.node().gate) for g in asks)))
    return out


def _numbered(items: list[str]) -> list[str]:
    return [_more(f"{_dim(f'{i}.')} {s}") for i, s in enumerate(items, 1)]


def _plan(eng: Engine) -> list[str]:
    t = eng.art("plan")
    out = [_key("Summary", t.get("summary", "")), ""]
    if t.get("acceptance_criteria"):
        out.append(_key("Criteria"))
        out += [_more(f"{typer.style('□', fg='cyan')} {c}") for c in t["acceptance_criteria"]]
    if t.get("plan"):
        out += ["", _key("Plan"), *_numbered(t["plan"])]
    return out


def _pr(eng: Engine) -> list[str]:
    p = eng.out("pr_preview")
    out = [_key("Title", typer.style(p.get("title", ""), bold=True)), _key("Preview", _dim(f"sha {p.get('sha')}"))]
    if to := eng.cfg.tracker.transitions.get("review"):
        out.append(_key("Ticket", f"moves to “{to}” when the PR opens"))
    body = Path(p.get("file", "")).read_text().splitlines() if p.get("file") and Path(p["file"]).is_file() else []
    if body:
        out += ["", _key("Body")] + [_more(_dim("│ ") + ln) for ln in body[:18]]
        if len(body) > 18:
            out.append(_more(_dim(f"│ … {len(body) - 18} more lines in {p['file']}")))
    return out


_SECTIONS: dict[str, Any] = {"plan": _plan, "pr": _pr}


def render(eng: Engine, gate: str) -> str:
    body = _SECTIONS[gate](eng) if gate in _SECTIONS else ["  " + ln for ln in eng.gate_summary(gate).splitlines()]
    if gate in _SECTIONS:  # pr: the only gate a code ticket stops at
        body += ["", *risk_block(eng)]
    hint = _dim(f"  reject      factory reject {gate} --reason …")
    return "\n".join([header(eng, gate), "", *body, "", hint])


def confirm_prompt(gate: str, code: str) -> str:
    return (
        "\n  "
        + typer.style("◆", fg="yellow", bold=True)
        + f" Type {typer.style(code, fg='yellow', bold=True)} to approve"
        + _dim(" (Ctrl-C to cancel)")
    )


def approved(gate: str) -> str:
    return "  " + typer.style(f"✓ Gate '{gate}' approved", fg="green", bold=True) + _dim(" — continuing") + "\n"

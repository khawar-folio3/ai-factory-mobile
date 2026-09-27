from __future__ import annotations

import re
from pathlib import Path

from conftest import FakePlatform, load
from test_pipeline import edit, to_work, work

from mobile_factory import gateview
from mobile_factory.pipeline import Engine

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def test_pr_gate_shows_the_preview_risk_and_why_it_asks(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=2), "APP-1")
    to_work(eng)
    edit(repo)
    eng.submit("work", work(eng))
    out = plain(gateview.render(eng, "pr"))
    assert out.startswith("▌ Open the draft PR   APP-1 · gate 'pr'")
    assert "Title       APP-1: Make the profile avatar 64dp" in out and "│ ## Acceptance criteria" in out
    assert re.search(r"Risk\s+█*░+\s+\d+/100\s+low", out) and "  +3  1 file(s) changed" in out
    assert "Autonomy    L2 supervised  (risk allows L4, capped by your ceiling L2)" in out
    assert "asks you at: pr" in out and "factory reject pr" in out


def test_confirm_prompt_and_approved_line() -> None:
    assert plain(gateview.confirm_prompt("diff", "875f")).strip() == "◆ Type 875f to approve (Ctrl-C to cancel)"
    assert plain(gateview.approved("diff")).strip() == "✓ Gate 'diff' approved — continuing"

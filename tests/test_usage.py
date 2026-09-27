from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mobile_factory import usage
from mobile_factory.state import RunState


def _msg(mid: str, model: str, ts: datetime, out: int, **extra: object) -> str:
    u = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": out}
    return json.dumps(
        {"type": "assistant", "timestamp": ts.isoformat(), "message": {"id": mid, "model": model, "usage": u}, **extra}
    )


def test_run_tokens_come_from_its_driving_sessions_and_their_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage.Path, "home", lambda: tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    proj = usage.projects_dir(root)
    proj.mkdir(parents=True)
    t0 = datetime(2026, 9, 27, 10, tzinfo=UTC)
    st = RunState(
        id="20260927-APP-1-abcd",
        ticket="APP-1",
        created_at=t0.isoformat(),
        updated_at=(t0 + timedelta(hours=1)).isoformat(),
        node="handoff",
        ceiling=2,
        status="done",
    )
    driver = proj / "s1.jsonl"
    driver.write_text(
        "\n".join(
            [
                _msg("a", "claude-opus-5-5", t0 - timedelta(minutes=5), 999),  # before the run: not counted
                json.dumps({"type": "user", "message": {"content": f"RUN .../{st.id}"}}),
                _msg("b", "claude-opus-5-5", t0 + timedelta(minutes=1), 50),
                _msg("b", "claude-opus-5-5", t0 + timedelta(minutes=1), 50),  # same response, second content block
            ]
        )
        + "\n"
    )
    (proj / "s1/subagents").mkdir(parents=True)
    (proj / "s1/subagents/agent-x.jsonl").write_text(_msg("c", "claude-haiku-4-5", t0 + timedelta(minutes=2), 7) + "\n")
    (proj / "other.jsonl").write_text(
        _msg("d", "claude-opus-5-5", t0 + timedelta(minutes=3), 500) + "\n"
    )  # another session
    u = usage.for_run(st, root)
    assert u.sessions == ["s1"]
    assert u.by_model == {
        "opus": {"input": 2, "cache_write": 100, "cache_read": 1000, "output": 50},
        "haiku": {"input": 2, "cache_write": 100, "cache_read": 1000, "output": 7},
    }
    assert u.total == 1152 + 1109
    assert "tokens: 2,261 total across 1 session(s)" in usage.render(u)


def test_projects_dir_matches_claude_code_naming(tmp_path: Path) -> None:
    p = usage.projects_dir(Path("/Users/k/android_two/cx_app"))
    assert p.name == "-Users-k-android-two-cx-app"

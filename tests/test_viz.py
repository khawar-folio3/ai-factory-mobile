from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from mobile_factory import events, viz
from mobile_factory.cli import app
from mobile_factory.config import VizConfig
from mobile_factory.viz.pixel import PixelAgents

runner = CliRunner()


class FakeViz(viz.Visualizer):
    """A visualiser that records what it is asked to show: how any backend is tested without a real tool."""

    name = "fake"
    title = "Fake office"
    sent: ClassVar[list[tuple[str, str, dict[str, Any]]]] = []

    def offices(self) -> list[viz.Office]:
        return [viz.Office("http://127.0.0.1:1/", 1)]

    def send(self, root: Path, session: str, kind: viz.Kind, **fields: Any) -> None:
        self.sent.append((session, kind, fields))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeViz]:
    monkeypatch.setitem(viz.BACKENDS, "fake", FakeViz)
    FakeViz.sent.clear()
    return FakeViz


def test_make_picks_the_backend_or_a_silent_one() -> None:
    assert isinstance(viz.make(VizConfig()), viz.NullVisualizer)  # off
    assert isinstance(viz.make(VizConfig(pixel_agents=True, tool="nope")), viz.NullVisualizer)  # unknown
    assert isinstance(viz.make(VizConfig(pixel_agents=True)), PixelAgents)


def test_a_broken_backend_never_breaks_the_work(tmp_path: Path) -> None:
    class Broken(viz.Visualizer):
        def send(self, root: Path, session: str, kind: viz.Kind, **fields: Any) -> None:
            raise RuntimeError("office on fire")

    s = viz.Session(Broken(), tmp_path, "x")
    s.begin()
    s.step("a")
    s.end()
    viz.event_sink(Broken(), tmp_path)({"type": events.RUN_STARTED, "run": "r1"})


def test_factory_run_events_become_one_session(fake: type[FakeViz], tmp_path: Path) -> None:
    sink = viz.event_sink(FakeViz(), tmp_path)
    for ev in (
        {"type": events.RUN_STARTED, "run": "r1"},
        {"type": events.NODE_STARTED, "run": "r1", "node": "fix", "title": "Fix"},
        {"type": events.GATE_WAITING, "run": "r1", "gate": "diff"},
        {"type": events.NODE_COMPLETED, "run": "r1", "node": "fix"},
        {"type": "unrelated", "run": "r1"},
        {"type": events.RUN_FINISHED, "run": "r1", "outcome": "draft-pr"},
    ):
        sink(ev)
    assert [(s, k) for s, k, _ in fake.sent] == [
        ("run-r1", "begin"),
        ("run-r1", "step"),
        ("run-r1", "waiting"),
        ("run-r1", "step_done"),
        ("run-r1", "end"),
    ]
    assert fake.sent[1][2]["title"] == "Fix" and fake.sent[-1][2]["outcome"] == "draft-pr"


def test_pixel_payloads(tmp_path: Path) -> None:
    p = PixelAgents.payloads
    start = p(tmp_path, "r1", "step", title="Reading review comments", short="comments")[0]
    assert start["hook_event_name"] == "PreToolUse" and start["tool_name"] == "comments"  # "Using comments"
    assert start["session_id"] == "factory-r1" and start["cwd"] == str(tmp_path)
    helper = p(tmp_path, "r1", "subagent", title="tally 1")[0]
    assert helper["tool_name"] == "Agent" and helper["tool_input"]["description"] == "tally 1"  # "Subtask: tally 1"
    assert p(tmp_path, "r1", "waiting")[0]["notification_type"] == "permission_prompt"
    assert [x["hook_event_name"] for x in p(tmp_path, "r1", "end", outcome="done")] == ["Stop", "SessionEnd"]
    assert [x["hook_event_name"] for x in p(tmp_path, "r1", "begin")] == ["SessionStart", "PostToolUse"]


def test_pixel_without_servers_is_silent(tmp_path: Path) -> None:
    PixelAgents().send(tmp_path, "r1", "begin")
    assert PixelAgents().offices() == []


def test_pixel_live_hooks_detection(tmp_path: Path) -> None:
    px = PixelAgents()
    assert not px.live_hooks(tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": {}}))
    assert not px.live_hooks(tmp_path)
    hook = {"PreToolUse": [{"hooks": [{"type": "command", "command": "node ~/.pixel-agents/hooks/claude-hook.js"}]}]}
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": hook}))
    assert px.live_hooks(tmp_path)


def test_pixel_url_carries_the_token_that_allows_hooks() -> None:
    assert PixelAgents.url({"port": 62037, "token": "abc"}) == "http://127.0.0.1:62037/?token=abc"
    assert PixelAgents.url({"port": 62037}) == "http://127.0.0.1:62037/"


def test_pixel_prefers_labels_and_grants_hook_consent() -> None:
    f = PixelAgents.home / "config.json"
    f.write_text(json.dumps({"standalone": {"soundEnabled": True}, "hooksConsent": {}, "hooksEnabled": {}}))
    PixelAgents().prefer(labels=True, hooks=True)
    cfg = json.loads(f.read_text())
    assert cfg["standalone"] == {"soundEnabled": True, "alwaysShowLabels": True}  # other settings kept
    assert cfg["hooksConsent"] == {"claude": "granted"} and cfg["hooksEnabled"] == {"claude": True}
    f.write_text(json.dumps({"hooksConsent": {}}))
    PixelAgents().prefer(labels=True, hooks=False)
    assert json.loads(f.read_text())["hooksConsent"] == {}  # no consent unless the developer wants hooks


def test_viz_demo_runs_without_a_factory_setup(
    fake: type[FakeViz], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # not a set-up repo, not even a git repo
    r = runner.invoke(app, ["viz", "demo", "--tool", "fake", "--steps", "3", "--subagents", "2", "--seconds", "0.2"])
    assert r.exit_code == 0, r.output
    kinds = [k for _, k, _ in fake.sent]
    assert kinds[0] == "begin" and kinds[-1] == "end"
    assert kinds.count("subagent") == kinds.count("subagent_done") == 2
    assert kinds.count("step") == kinds.count("step_done") == 3
    assert "watch it: http://127.0.0.1:1/" in r.output


def test_viz_status_and_unknown_tool(fake: type[FakeViz], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["viz", "status", "--tool", "fake"])
    assert r.exit_code == 0 and "Fake office" in r.output and "http://127.0.0.1:1/" in r.output
    r = runner.invoke(app, ["viz", "status", "--tool", "nope"])
    assert r.exit_code != 0


def test_labels_stay_short_so_they_do_not_overlap(fake: type[FakeViz], tmp_path: Path) -> None:
    from mobile_factory.viz.base import LABEL_MAX, label

    assert label("tally") == "tally"
    assert label("Reading review comments") == "Reading"
    assert len(label("Supercalifragilisticexpialidocious")) == LABEL_MAX
    s = viz.Session(FakeViz(), tmp_path, "x")
    s.step("Tallying review chunks", "tally")
    s.step("Resolving review threads")
    s.subagent("a very long subagent description")
    shorts = [f.get("short") or f.get("title") for _, k, f in fake.sent if k in ("step", "subagent")]
    assert shorts == ["tally", "Resolving", "a"] and all(len(x) <= LABEL_MAX for x in shorts)


def test_viz_demo_parallel_sessions_each_begin_and_end(
    fake: type[FakeViz], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(
        app,
        ["viz", "demo", "--tool", "fake", "--steps", "1", "--subagents", "0", "--seconds", "0.2", "--sessions", "4"],
    )
    assert r.exit_code == 0, r.output
    ids = {sid for sid, _, _ in fake.sent}
    assert len(ids) == 5  # main + 4 workers, each its own session (its own desk)
    for sid in ids:
        kinds = [k for s, k, _ in fake.sent if s == sid]
        assert kinds[0] == "begin" and kinds[-1] == "end"

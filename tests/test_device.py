from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import FakePlatform, load
from typer.testing import CliRunner

from mobile_factory import events, timeline
from mobile_factory.cli import app
from mobile_factory.config import AndroidConfig
from mobile_factory.errors import FactoryError
from mobile_factory.pipeline import Engine
from mobile_factory.platforms import android, maestro
from mobile_factory.platforms.android import Android

runner = CliRunner()


@pytest.fixture
def at_work(repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("mobile_factory.cli.make_platform", lambda lc: fake)
    eng = Engine.start(load(repo), "APP-1")
    eng.advance()
    assert eng.st.node == "work"
    return eng


def drive(fake: FakePlatform) -> None:
    assert runner.invoke(app, ["android", "install"]).exit_code == 0
    fake.last_hit = {"text": "Spaces (3)", "id": "com.example:id/tab", "desc": "", "point": "50,900"}
    assert runner.invoke(app, ["android", "tap", "Spaces"]).exit_code == 0
    fake.last_hit = {"text": "", "id": "com.example:id/search", "desc": "", "point": "10,10"}
    assert runner.invoke(app, ["android", "tap", "search", "--nth", "2"]).exit_code == 0
    assert runner.invoke(app, ["android", "snap", "before", "spaces"]).exit_code == 0
    fake.last_hit = {"point": "300,40"}
    assert runner.invoke(app, ["android", "tap", "⋮"]).exit_code == 0
    assert runner.invoke(app, ["android", "back"]).exit_code == 0


FLOW = """appId: com.example.demo
---
- launchApp
- tapOn: {text: "Spaces \\\\(3\\\\)"}
- tapOn: {id: "com.example:id/search", index: 1}
# snap before: spaces
- takeScreenshot: "before-spaces"
- tapOn: {point: "300,40"}
- back
"""


def test_recorder_writes_valid_maestro_from_selectors(at_work: Engine, fake: FakePlatform) -> None:
    drive(fake)
    flow = at_work.flow("work")
    assert flow.read_text() == FLOW
    head, steps = yaml.safe_load_all(flow.read_text())
    assert head == {"appId": "com.example.demo"}
    assert steps[1] == {"tapOn": {"text": "Spaces \\(3\\)"}} and steps[2]["tapOn"]["index"] == 1


def test_every_device_command_is_timed_into_the_run(
    at_work: Engine, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive(fake)
    monkeypatch.setattr(fake, "tap", lambda label, nth=1: (_ for _ in ()).throw(FactoryError(f"no element {label}")))
    assert runner.invoke(app, ["android", "tap", "Gone"]).exit_code == 1
    recs = timeline.read_device(at_work.dir)
    assert [r["command"] for r in recs] == ["install", "tap", "tap", "snap", "tap", "back", "tap"]
    assert all(r["step"] == "work" and r["duration_ms"] >= 0 and "ts" in r for r in recs)
    assert recs[1]["args"] == {"label": "Spaces", "nth": 1} and recs[1]["ok"] and recs[1]["result"] == "tapped Spaces"
    assert not recs[-1]["ok"] and recs[-1]["result"] == "no element Gone"
    assert "Gone" not in at_work.flow("work").read_text()  # a failed tap is not recorded


def test_device_commands_work_without_a_run(repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("mobile_factory.cli.make_platform", lambda lc: fake)
    assert runner.invoke(app, ["android", "tap", "Spaces"]).output == "tapped Spaces\n"


def test_node_events_carry_durations_and_log_sums_them(at_work: Engine) -> None:
    done = [
        e for e in events.read(at_work.lc.state_dir / "events.jsonl", at_work.st.id) if e["type"] == "node.completed"
    ]
    assert done and all(isinstance(e["duration_ms"], int) for e in done)

    evs = [
        {"ts": "2026-09-27T10:00:00+00:00", "type": "node.completed", "node": "triage", "duration_ms": 120_000},
        {"ts": "2026-09-27T10:02:00+00:00", "type": "node.completed", "node": "branch", "duration_ms": 1_000},
        {"ts": "2026-09-27T10:10:00+00:00", "type": "node.completed", "node": "reproduce", "duration_ms": 300_000},
        {"ts": "2026-09-27T10:11:00+00:00", "type": "node.failed", "node": "checks", "duration_ms": 90_000},
        {"ts": "2026-09-27T10:13:00+00:00", "type": "node.completed", "node": "checks", "duration_ms": 60_000},
        {"ts": "2026-09-27T10:20:00+00:00", "type": "gate.waiting", "node": "pr_preview", "gate": "pr"},
        {"ts": "2026-09-27T10:23:00+00:00", "type": "gate.decided", "node": "pr_preview", "gate": "pr"},
        {"ts": "2026-09-27T10:23:00+00:00", "type": "node.completed", "node": "pr_preview", "duration_ms": 182_000},
        {
            "ts": "2026-09-27T10:23:00+00:00",
            "type": "node.completed",
            "node": "baseline",
            "skipped": True,
            "duration_ms": 0,
        },
    ]
    dev = [
        {
            "ts": "2026-09-27T10:03:00+00:00",
            "step": "reproduce",
            "command": "install",
            "args": {},
            "duration_ms": 80_000,
            "ok": True,
        },
        {
            "ts": "2026-09-27T10:05:00+00:00",
            "step": "reproduce",
            "command": "tap",
            "args": {"label": "Spaces"},
            "duration_ms": 4_000,
            "ok": True,
        },
        {
            "ts": "2026-09-27T10:06:00+00:00",
            "step": "reproduce",
            "command": "wait",
            "args": {"text": "Room"},
            "duration_ms": 16_000,
            "ok": False,
            "result": "timeout waiting for: Room",
        },
    ]
    kinds = {"triage": "agent", "reproduce": "agent", "branch": "auto", "checks": "gradle", "pr_preview": "auto"}
    sts = timeline.steps(evs, dev, kinds)
    rep = next(s for s in sts if s.step == "reproduce")
    assert (rep.total_ms, rep.gradle_ms, rep.device_ms, rep.rest_ms, len(rep.cmds)) == (
        300_000,
        80_000,
        20_000,
        200_000,
        3,
    )
    assert next(s for s in sts if s.step == "checks").gradle_ms == 150_000  # both attempts
    assert timeline.summary(sts) == {
        "agent": 320_000,
        "device": 20_000,
        "gradle": 230_000,
        "waiting on human": 180_000,
        "runner": 3_000,
    }
    table = timeline.render_summary(sts)
    assert "agent            |    5m20s |  42%" in table and "total            |   12m33s |" in table
    lines = timeline.render_steps(sts)
    assert "baseline       skipped" in lines and "✗ wait 'Room': timeout waiting for: Room" in lines
    assert "wait 'Room' 16.0s" in lines.splitlines()[3]


def test_status_timeline_prints_where_the_time_went(at_work: Engine, fake: FakePlatform) -> None:
    drive(fake)
    out = runner.invoke(app, ["status", "--timeline", "--summary"]).output
    assert out.startswith("category") and "device" in out and "waiting on human" in out
    assert "work" in runner.invoke(app, ["status", "--timeline"]).output
    step = runner.invoke(app, ["status", "--step", "work"]).output
    assert step.startswith("work:") and "tap 'Spaces'" in step and "snap 'before'" in step


def test_replay_splits_at_snaps_and_logs_each_step(at_work: Engine, fake: FakePlatform) -> None:
    drive(fake)
    r = runner.invoke(app, ["android", "replay", "work", "--snap", "after"])
    assert r.exit_code == 0, r.output
    assert len(fake.flows) == 2 and "takeScreenshot" not in "".join(fake.flows)
    assert fake.flows[0].endswith('tapOn: {id: "com.example:id/search", index: 1}\n')
    assert (at_work.dir / "snapshots/after/spaces.txt").is_file()
    rec = timeline.read_device(at_work.dir)[-1]
    assert rec["command"] == "replay" and [s["step"] for s in rec["steps"]] == [
        "flow work.part1",
        "snap after spaces",
        "flow work.part2",
    ]
    assert "✓ 1.2s flow work.part1" in r.output


def test_saved_flows_replay_by_name(at_work: Engine, fake: FakePlatform) -> None:
    lib = at_work.library() / "spaces-search.yaml"
    lib.parent.mkdir(parents=True)
    lib.write_text(FLOW)
    assert not str(lib).startswith(str(at_work.lc.root))  # zero footprint in the app repo
    assert runner.invoke(app, ["android", "replay", "spaces-search"]).exit_code == 0
    assert "no flow 'nope'" in str(runner.invoke(app, ["android", "replay", "nope"]).exception)


def test_android_replay_runs_maestro_in_the_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], cwd: Path | None = None, **kw: object) -> object:
        seen.update(cmd=cmd, cwd=cwd)
        dbg = Path(cmd[cmd.index("--debug-output") + 1])
        (dbg / "commands-flow.json").write_text(
            json.dumps(
                [
                    {
                        "command": {"launchAppCommand": {"appId": "com.acme"}},
                        "metadata": {"status": "COMPLETED", "duration": 2100},
                    },
                    {
                        "command": {"tapOnElement": {"selector": {"textRegex": "Spaces"}}},
                        "metadata": {"status": "COMPLETED", "duration": 800},
                    },
                    {
                        "command": {"assertConditionCommand": {"condition": {"visible": {"textRegex": "Room"}}}},
                        "metadata": {"status": "FAILED", "duration": 5000, "error": {"message": "not visible"}},
                    },
                    {"command": {"backPressCommand": {}}, "metadata": {"status": "PENDING"}},
                ]
            )
        )
        return type("R", (), {"ok": False, "out": "", "err": "boom"})()

    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    monkeypatch.setattr(maestro, "which", lambda tool: None)
    monkeypatch.setattr(maestro, "HOME_BIN", tmp_path / "maestro")
    assert maestro.binary() is None
    (tmp_path / "maestro").write_text("")
    assert maestro.binary() == str(tmp_path / "maestro")  # ~/.maestro/bin fallback
    monkeypatch.setattr(android, "run", fake_run)
    flow = tmp_path / "f.yaml"
    flow.write_text("appId: com.acme\n---\n- launchApp\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    res = Android(tmp_path / "repo", AndroidConfig()).run_flow(flow, logs)
    assert seen["cwd"] == logs and seen["cmd"][:3] == [str(tmp_path / "maestro"), "test", "--debug-output"]  # type: ignore[index]
    assert not res.ok and res.steps == [
        {"step": "launchApp", "ok": True, "duration_ms": 2100, "seq": 0},
        {"step": "tapOnElement Spaces", "ok": True, "duration_ms": 800, "seq": 1},
        {"step": "assertCondition Room", "ok": False, "duration_ms": 5000, "seq": 2, "error": "not visible"},
    ]


def test_maestro_steps_read_the_2x_commands_json(tmp_path: Path) -> None:
    d = tmp_path / ".maestro" / "tests" / "2026-09-27_215254" / "flow"
    d.mkdir(parents=True)
    (d / "commands.json").write_text(
        json.dumps(
            [
                {"command": {"defineVariablesCommand": {}}, "metadata": {"status": "COMPLETED", "duration": 6}},
                {
                    "command": {"openLinkCommand": {"link": "demo://dl/p"}},
                    "metadata": {"status": "COMPLETED", "duration": 900},
                },
            ]
        )
    )
    assert maestro.steps(tmp_path) == [{"step": "openLink demo://dl/p", "ok": True, "duration_ms": 900, "seq": 0}]


def test_taps_are_not_recorded_into_an_authored_flow(at_work: Engine, fake: FakePlatform) -> None:
    flow = at_work.flow("work")
    flow.parent.mkdir(parents=True, exist_ok=True)
    authored = "appId: com.example.demo\n---\n# criterion 1: Spaces shows\n- assertVisible: Spaces\n"
    flow.write_text(authored)
    assert runner.invoke(app, ["android", "tap", "Spaces"]).exit_code == 0
    assert flow.read_text() == authored

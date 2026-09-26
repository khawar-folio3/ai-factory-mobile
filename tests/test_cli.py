from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakePlatform
from typer.testing import CliRunner

from mobile_factory import config
from mobile_factory.cli import app
from mobile_factory.platforms.base import CheckRun

runner = CliRunner()


@pytest.fixture
def in_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("FACTORY_SECRETS_FILE", str(repo.parent / "secrets.env"))
    return repo


def test_init_detects_android_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings.gradle.kts").write_text('include(":app", ":core:ui")\n')
    (tmp_path / "app/src/main").mkdir(parents=True)
    (tmp_path / "app/build.gradle.kts").write_text(
        'plugins { alias(libs.plugins.android.application) }\nandroid { defaultConfig { applicationId = "com.acme.app" } }\n'
    )
    (tmp_path / "app/src/main/AndroidManifest.xml").write_text(
        '<manifest><application><activity android:name=".Other"/>'
        '<activity android:name=".MainActivity"><intent-filter>'
        '<category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity></application></manifest>'
    )
    monkeypatch.setattr("mobile_factory.init.has", lambda tool: False)
    r = runner.invoke(app, ["init", "--defaults"])
    assert r.exit_code == 0, r.output
    text = config.config_path(tmp_path).read_text()
    assert "kind: file" in text
    assert 'application_id: "com.acme.app"' in text
    assert 'launch_activity: ".MainActivity"' in text
    assert 'modules: ["app", "core/ui"]' in text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app", "settings.gradle.kts"]  # repo untouched
    assert runner.invoke(app, ["init", "--defaults"]).exit_code != 0


def test_init_uses_twg_login_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    (home / ".config/twg").mkdir(parents=True)
    (home / ".config/twg/auth.conf").write_text("user=a@b.c\ntoken=s3cr3tvalue\nsite=acme\n")
    monkeypatch.setattr("mobile_factory.init.Path.home", lambda: home)
    monkeypatch.setattr("mobile_factory.init.has", lambda tool: tool == "twg")
    assert runner.invoke(app, ["init", "--defaults"]).exit_code == 0
    text = config.config_path(tmp_path).read_text()
    assert "kind: jira" in text and 'site: "acme.atlassian.net"' in text and "s3cr3tvalue" not in text
    lc = config.load(tmp_path)
    assert lc.cfg.tracker.provider == "twg"
    assert config.env_refs(lc.raw) == set()  # no secret needed by default


def test_doctor_offline(in_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mobile_factory.doctor.make_platform", lambda lc: FakePlatform())
    r = runner.invoke(app, ["doctor", "--offline"])
    assert "python >= 3.11" in r.output and "fake device" in r.output
    assert "taste rules" in r.output


def test_schema_and_unknown_node(in_repo: Path) -> None:
    r = runner.invoke(app, ["schema", "fix"])
    assert r.exit_code == 0 and json.loads(r.output)["title"] == "FixOut"
    r = runner.invoke(app, ["schema", "publish"])
    assert r.exit_code != 0


def test_run_submit_and_gate_via_cli(in_repo: Path, fake: FakePlatform) -> None:
    cfg = config.config_path(in_repo)
    cfg.write_text(cfg.read_text().replace("ceiling: 4", "ceiling: 0"))  # outside the repo: nothing to commit
    r = runner.invoke(app, ["run", "APP-1"])
    assert r.exit_code == 0, r.output
    assert "TASK" in r.output and "factory submit triage" in r.output

    triage = in_repo.parent / "triage.json"
    triage.write_text(
        json.dumps({"verdict": "eligible", "reason": "clear", "summary": "Avatar clipped", "estimated_files": 1})
    )
    r = runner.invoke(app, ["submit", "triage", str(triage)])
    assert "WAITING ON A HUMAN at gate 'plan'" in r.output

    r = runner.invoke(app, ["gate"])
    assert "Plan for APP-1" in r.output
    r = runner.invoke(app, ["approve", "plan"])  # CliRunner is not a TTY
    assert r.exit_code != 0

    assert "risk" in runner.invoke(app, ["risk"]).output
    assert "APP-1" in runner.invoke(app, ["status", "--all"]).output
    assert "runs 1" in runner.invoke(app, ["metrics"]).output
    evs = runner.invoke(app, ["events"]).output.splitlines()
    assert json.loads(evs[0])["type"] == "run.started"

    r = runner.invoke(app, ["abort", "--reason", "test"])
    assert "✕" in r.output


def test_guardrail_check(in_repo: Path) -> None:
    f = in_repo.parent / "findings.json"
    f.write_text(json.dumps([{"file": "a.kt", "line": 1, "rule": "R1", "severity": "blocker", "suggestion": "x"}]))
    r = runner.invoke(app, ["guardrail", "check", str(f)])
    assert "NEEDS WORK" in r.output


def test_secrets_list_never_prints_values(in_repo: Path) -> None:
    (in_repo.parent / "secrets.env").write_text("JIRA_API_TOKEN=supersecretvalue\n")
    r = runner.invoke(app, ["secrets", "list"])
    assert "JIRA_API_TOKEN" in r.output and "supersecretvalue" not in r.output
    assert runner.invoke(app, ["secrets", "set", "X"]).exit_code != 0  # needs a TTY


def test_android_install_command(in_repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.invoke(app, ["run", "APP-1"])
    monkeypatch.setattr(FakePlatform, "build_install", lambda self, d, launch=True: CheckRun(False, "BUILD FAILED"))
    r = runner.invoke(app, ["android", "install"])
    assert r.exit_code == 1 and "BUILD FAILED" in r.output


def test_launcher_found_in_custom_source_set_ignoring_comments(tmp_path: Path) -> None:
    from mobile_factory.init import _launcher

    (tmp_path / "src").mkdir()
    (tmp_path / "build.gradle").write_text("android { namespace 'com.acme' }\n")
    (tmp_path / "src/AndroidManifest.xml").write_text(
        """<manifest><application>
        <!--<activity android:name="com.old.Snip">
            <category android:name="android.intent.category.LAUNCHER" /></activity>-->
        <activity android:name=".splash.Launch">
            <intent-filter><category android:name="android.intent.category.LAUNCHER" /></intent-filter>
        </activity></application></manifest>"""
    )
    assert _launcher(tmp_path) == "com.acme.splash.Launch"


def test_distill_runs_one_session_per_chunk_then_a_merge(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mobile_factory import cli, config

    lc = config.load(repo)
    data = lc.state_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "reviews.jsonl").write_text("".join(f'{{"id": {n}, "pr": {n}}}\n' for n in range(250)))  # 3 chunks
    batches: list[list[list[str]]] = []

    def fake_run(cmds: list[list[str]], cwd: Path, logs: list[Path], stage: object, **_: object) -> int:
        batches.append(cmds)
        if len(batches) == 1:  # the tally sessions each write their file
            for n in range(1, len(cmds) + 1):
                (data / f"tally-{n}.json").write_text("{}")
        else:
            lc.path(lc.cfg.guardrail.taste).write_text("### R001 · rule [nit]\n")
        return 0

    monkeypatch.setattr(cli, "has", lambda tool: tool == "claude")
    monkeypatch.setattr(cli, "_run_watched", fake_run)
    assert cli._distill(lc)
    tallies, (merge,) = batches
    assert len(tallies) == 3  # separate sessions: each gets its own desk in the office
    assert all(c[:2] == ["claude", "-p"] and "--agents" not in c for c in [*tallies, merge])
    assert "Tally lines 1-100" in tallies[0][2] and tallies[0][tallies[0].index("--model") + 1] == "sonnet"
    assert "steps 1-3 are done" in merge[2] and "Guardrail learn" in merge[2]
    assert "Agent" not in merge[merge.index("--allowedTools") + 1]  # no subagents any more


def test_distill_without_agent_cli_says_so(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mobile_factory import cli, config

    monkeypatch.setattr(cli, "has", lambda tool: False)
    assert not cli._distill(config.load(repo))


def test_progress_prints_phases_when_not_a_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    from mobile_factory.cli import _Progress

    bar = _Progress()
    for i in range(4):
        bar("Reading review comments", i, 3)
    bar("Resolving review threads", 0, 0)
    bar.close()
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "  Reading review comments…",
        "  ✓ Reading review comments (3)",
        "  Resolving review threads…",
        "  ✓ Resolving review threads",
    ]


def test_quiet_install_logs_output_and_shows_tail_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mobile_factory import cli, config
    from mobile_factory.setup import Step

    monkeypatch.setattr(config, "secrets_path", lambda *_: tmp_path / "secrets.env")
    assert cli._install_quietly(Step("demo", "install", "echo noisy-progress")) == 0
    out = capsys.readouterr().out
    assert "noisy-progress" not in out and "✓ installed demo" in out
    assert "noisy-progress" in (tmp_path / "logs/install-demo.log").read_text()

    assert cli._install_quietly(Step("demo", "install", "echo boom; exit 3")) == 3
    out = capsys.readouterr().out
    assert "✗ installing demo failed" in out and "boom" in out and "run it yourself: echo boom; exit 3" in out


def test_run_watched_drives_progress_from_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mobile_factory import cli

    out = tmp_path / "done.txt"
    stage = lambda: ("Writing taste rules", 0, 0) if out.exists() else ("Tallying review chunks", 0, 2)  # noqa: E731
    code = cli._run_watched(
        [["sh", "-c", f"sleep 0.5; echo x > {out}"], ["sh", "-c", "sleep 0.2"]],
        tmp_path,
        [tmp_path / "a.log", tmp_path / "b.log"],
        stage,
    )
    lines = capsys.readouterr().out.splitlines()
    assert code == 0 and lines[0] == "  Tallying review chunks…" and "✓ Writing taste rules" in lines[-1]


def test_office_opt_in_is_saved_per_developer(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    from mobile_factory import cli, config

    lc = config.load(repo)
    lc.path(lc.cfg.guardrail.taste).write_text("rules\n")  # taste done: only the office question is left
    monkeypatch.setattr(cli, "has", lambda tool: True)
    monkeypatch.setattr(cli, "_yes", lambda question, default: "office" in question or "live hooks" in question)
    started: list[Path] = []
    monkeypatch.setattr(cli, "_start_office", started.append)
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: 0)  # keep the taste rules
    cli._optional_extras(repo)
    assert started == [repo]
    assert yaml.safe_load(config.local_path(repo).read_text())["viz"] == {"pixel_agents": True, "pixel_hooks": True}
    assert not (repo / ".factory/local.yaml").exists()  # nothing personal inside the repo
    assert config.load(repo).cfg.viz.pixel_agents
    assert "pixel_agents: true" not in config.config_path(repo).read_text()  # only in this developer's local.yaml


def test_extend_init_asks_visualisation_before_the_long_taste_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mobile_factory import cli

    asked: list[str] = []
    monkeypatch.setattr(cli, "has", lambda tool: tool != "maestro")
    monkeypatch.setattr(cli, "_yes", lambda question, default: asked.append(question) or False)
    cli._optional_extras(repo)
    assert [q.split()[0] for q in asked] == ["Watch", "Install", "Harvest"]
    out = capsys.readouterr().out
    assert out.index("1 · Visualisation") < out.index("2 · UI flows") < out.index("3 · Taste rules")


@pytest.mark.parametrize(("pick", "full", "refresh"), [(1, False, True), (2, True, False)])
def test_existing_taste_rules_can_be_refreshed_or_rebuilt(
    repo: Path, monkeypatch: pytest.MonkeyPatch, pick: int, full: bool, refresh: bool
) -> None:
    from mobile_factory import cli, config

    lc = config.load(repo)
    lc.path(lc.cfg.guardrail.taste).write_text("### R001 · a [nit]\n### R002 · b [major]\n")
    data = lc.state_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "reviews.jsonl").write_text('{"id": 1, "pr": 1}\n{"id": 2, "pr": 2}\n')
    (data / "distilled_ids.json").write_text('["1"]')  # comment 2 is new since the rules were built
    calls: dict[str, object] = {}
    meta = {"comments": 3, "prs_with_owner_comments": 2, "prs_scanned": 10, "changes_requested": 1, "owners": ["o"]}
    monkeypatch.setattr(cli, "has", lambda tool: True)
    monkeypatch.setattr(cli, "_start_office", lambda root: None)
    monkeypatch.setattr(cli, "_agent_cli", lambda lc: "claude")
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: pick)
    monkeypatch.setattr(cli, "_yes", lambda q, d: q.startswith("Update"))
    monkeypatch.setattr(cli, "_harvest", lambda lc, **kw: calls.setdefault("full", kw["full"]) is not None and meta)
    monkeypatch.setattr(cli, "_distill", lambda lc, refresh=False: calls.setdefault("refresh", refresh))
    cli._optional_extras(repo)
    assert calls == {"full": full, "refresh": refresh}


def test_declined_extras_are_not_asked_again(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mobile_factory import cli, config

    lc = config.load(repo)
    lc.path(lc.cfg.guardrail.taste).write_text("### R001 · a [nit]\n")
    asked: list[str] = []
    monkeypatch.setattr(cli, "has", lambda tool: tool != "maestro")
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: 0)  # keep the taste rules
    monkeypatch.setattr(cli, "_yes", lambda q, d: asked.append(q) or False)
    cli._optional_extras(repo)
    assert [q.split()[0] for q in asked] == ["Watch", "Install"]
    asked.clear()
    cli._optional_extras(repo)  # second init: both answers remembered
    assert asked == []


def test_long_work_shows_as_a_working_session_in_the_visualiser(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_viz import FakeViz

    from mobile_factory import cli, config, viz

    config.config_path(repo).write_text(
        config.config_path(repo).read_text() + "viz: {pixel_agents: true, tool: fake}\n"
    )
    monkeypatch.setitem(viz.BACKENDS, "fake", FakeViz)
    FakeViz.sent.clear()
    out = tmp_path / "done"
    stage = lambda: ("Writing taste rules", 0, 0) if out.exists() else ("Tallying review chunks", 0, 2)  # noqa: E731
    office = cli._watch(repo, "distill")
    assert office is not None
    assert (
        cli._run_watched([["sh", "-c", f"sleep 0.4; touch {out}"]], tmp_path, [tmp_path / "log"], stage, office=office)
        == 0
    )
    assert [(k, f.get("title", "")) for _, k, f in FakeViz.sent] == [
        ("begin", ""),
        ("step", "Tallying review chunks"),
        ("step_done", "Tallying review chunks"),
        ("step", "Writing taste rules"),
        ("step_done", "Writing taste rules"),
        ("end", ""),
    ]
    assert {sid for sid, _, _ in FakeViz.sent} == {office.id}


def test_refresh_with_nothing_new_skips_the_agent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mobile_factory import cli, config
    from mobile_factory.guardrail import harvest

    lc = config.load(repo)
    lc.path(lc.cfg.guardrail.taste).write_text("### R001 · a [nit]\n")
    data = lc.state_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "reviews.jsonl").write_text('{"id": 7, "pr": 1}\n')
    meta = {"comments": 1, "prs_with_owner_comments": 1, "prs_scanned": 1, "prs_new": 0, "changes_requested": 0}
    asked: list[str] = []
    monkeypatch.setattr(cli, "has", lambda tool: True)
    monkeypatch.setattr(cli, "_start_office", lambda root: None)
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: 1)  # refresh
    monkeypatch.setattr(cli, "_yes", lambda q, d: asked.append(q) or False)
    monkeypatch.setattr(cli, "_harvest", lambda lc, **kw: meta)
    monkeypatch.setattr(cli, "_distill", lambda lc, refresh=False: pytest.fail("no new comments: no distill"))
    cli._optional_extras(repo)
    assert not any(q.startswith("Update") for q in asked)
    assert harvest.undistilled(data) == []  # record bootstrapped: the rules already cover comment 7

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakePlatform, git
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
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    text = (tmp_path / "factory.yaml").read_text()
    assert "kind: file" in text
    assert 'application_id: "com.acme.app"' in text
    assert 'launch_activity: ".MainActivity"' in text
    assert 'modules: ["app", "core/ui"]' in text
    assert ".factory/runs/" in (tmp_path / ".gitignore").read_text()
    assert runner.invoke(app, ["init"]).exit_code != 0


def test_init_uses_twg_login_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    (home / ".config/twg").mkdir(parents=True)
    (home / ".config/twg/auth.conf").write_text("user=a@b.c\ntoken=s3cr3tvalue\nsite=acme\n")
    monkeypatch.setattr("mobile_factory.init.Path.home", lambda: home)
    monkeypatch.setattr("mobile_factory.init.has", lambda tool: tool == "twg")
    assert runner.invoke(app, ["init"]).exit_code == 0
    text = (tmp_path / "factory.yaml").read_text()
    assert "kind: jira" in text and 'site: "acme.atlassian.net"' in text and "s3cr3tvalue" not in text
    lc = config.load(tmp_path)
    assert lc.cfg.tracker.provider == "twg"
    assert config.env_refs(lc.raw) == {"SLACK_WEBHOOK_URL"}  # optional, has a default: nothing is required


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
    (in_repo / "factory.yaml").write_text((in_repo / "factory.yaml").read_text().replace("ceiling: 4", "ceiling: 0"))
    git(in_repo, "commit", "-q", "-am", "manual")
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

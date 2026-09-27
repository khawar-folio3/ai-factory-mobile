from __future__ import annotations

from pathlib import Path

import pytest

from mobile_factory import config
from mobile_factory.errors import ConfigError


def write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "factory.yaml").write_text(body)
    return tmp_path


def test_minimal_config_gets_defaults(tmp_path: Path) -> None:
    lc = config.load(write(tmp_path, "version: 1\nproject: {name: demo}\n"))
    assert lc.cfg.autonomy.ceiling == 2
    assert lc.cfg.gates["pr"].auto_at == 4
    assert "**/build.gradle*" in lc.cfg.project.forbidden_paths
    assert lc.cfg.steps == {}
    models = lc.cfg.agents.models
    assert models["spec"] == "opus" and "fix" not in models


def test_partial_gate_override_keeps_other_defaults(tmp_path: Path) -> None:
    lc = config.load(write(tmp_path, "version: 1\nproject: {name: d}\ngates: {pr: {auto_at: 5}}\n"))
    assert lc.cfg.gates["pr"].auto_at == 5
    assert lc.cfg.gates["plan"].auto_at == 1


def test_env_interpolation_from_secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("JIRA_API_TOKEN=abc123\nexport JIRA_EMAIL='me@example.com'\n")
    monkeypatch.setenv("FACTORY_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    body = 'version: 1\nproject: {name: d}\ntracker: {kind: jira, site: x.atlassian.net, email: "${JIRA_EMAIL}", token: "${JIRA_API_TOKEN}"}\n'
    lc = config.load(write(tmp_path, body))
    assert lc.cfg.tracker.token == "abc123"
    assert lc.cfg.tracker.email == "me@example.com"
    assert lc.missing_env == set()


def test_process_env_wins_and_missing_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("A=file\n")
    monkeypatch.setenv("FACTORY_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("A", "env")
    lc = config.load(write(tmp_path, 'version: 1\nproject: {name: "${A}-${NOPE}-${DEF:-x}"}\n'))
    assert lc.cfg.project.name == "env--x"
    assert lc.missing_env == {"NOPE"}


@pytest.mark.parametrize(
    "snippet",
    [
        'tracker: {token: "abc"}',
        'notifications: {slack_webhook: "https://hooks.slack.com/services/T0/B0/xyz"}',
        'mcp_servers: {gh: {url: "https://x", headers: {Authorization: "Bearer ghp_abcdefghijklmnopqrstuvwxyz0123"}}}',
    ],
)
def test_plaintext_secrets_are_refused(tmp_path: Path, snippet: str) -> None:
    with pytest.raises(ConfigError, match="plaintext secret"):
        config.load(write(tmp_path, f"version: 1\nproject: {{name: d}}\n{snippet}\n"))


def test_env_refs_are_allowed_for_secret_keys(tmp_path: Path) -> None:
    body = 'version: 1\nproject: {name: d}\nmcp_servers: {gh: {url: "https://x", headers: {Authorization: "Bearer ${GH}"}}}\n'
    lc = config.load(write(tmp_path, body))
    assert config.env_refs(lc.raw) == {"GH"}


def test_unknown_keys_fail_fast(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="autonmy"):
        config.load(write(tmp_path, "version: 1\nproject: {name: d}\nautonmy: {ceiling: 4}\n"))


def test_find_root_is_the_git_top_level_and_config_lives_outside(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write(tmp_path, "version: 1\nproject: {name: d}\n")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert config.find_root(deep) == tmp_path.resolve()
    assert not (tmp_path / "factory.yaml").exists()  # moved into the factory home on first use
    assert config.config_path(tmp_path).is_file() and config.state_home() in config.config_path(tmp_path).parents

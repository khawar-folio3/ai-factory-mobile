from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import git

from mobile_factory import config
from mobile_factory.errors import FactoryError
from mobile_factory.wizard import Checks, Wizard

D = None  # "take the default"


class Script:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.log: list[str] = []

    def _next(self, default: Any) -> Any:
        a = self.answers.pop(0)
        return default if a is None else a

    def say(self, text: str) -> None:
        self.log.append(text)

    def ask(self, question: str, default: str = "") -> str:
        return str(self._next(default))

    def secret(self, question: str) -> str:
        return str(self._next(""))

    def choose(self, question: str, options: list[str], default: int = 0) -> int:
        return int(self._next(default))

    def confirm(self, question: str, default: bool = True) -> bool:
        return bool(self._next(default))


class Fake(Checks):
    def __init__(self, tools: set[str] | None = None, accounts: list[str] | None = None, push: set[str] | None = None):
        self.tools = tools if tools is not None else {"twg", "gh"}
        self.accounts = accounts if accounts is not None else ["work", "client"]
        self.push = push if push is not None else {"client"}
        self.ran: list[str] = []
        self.rest_ok_after = 0
        self.twg_ok = True
        self.found: dict[str, str] = {}

    def has(self, tool: str) -> bool:
        return tool in self.tools

    def shell(self, cmd: str) -> int:
        self.ran.append(cmd)
        if "install-twg" in cmd:
            self.tools.add("twg")
        return 0

    def gh_accounts(self) -> list[str]:
        return self.accounts

    def gh_can_push(self, repo: str, account: str) -> bool:
        return account in self.push

    def twg_whoami(self, site: str) -> str:
        if not self.twg_ok:
            raise FactoryError("401")
        return f"twg as Dev <dev@{site}>"

    def jira_rest(self, site: str, email: str, token: str) -> str:
        if self.rest_ok_after > 0:
            self.rest_ok_after -= 1
            raise FactoryError("HTTP 401")
        return f"https://{site} as Dev"

    def figma_ready(self) -> bool:
        return True

    def remote_branches(self, root: Path) -> list[str]:
        return ["develop", "release/10-4", "main"]

    def has_flavors(self, root: Path) -> bool:
        return self.flavors

    def variants(self, root: Path, module: str) -> dict[str, str]:
        return self.found

    flavors = False


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "secrets.env"
    monkeypatch.setenv("FACTORY_SECRETS_FILE", str(f))
    return f


@pytest.fixture
def fresh(repo: Path) -> Path:
    config.config_path(repo).unlink()
    return repo


def local(repo: Path) -> dict[str, Any]:
    return yaml.safe_load(config.local_path(repo).read_text())


def test_first_time_repo_and_dev_with_twg(fresh: Path, secrets: Path) -> None:
    s = Script(
        1,  # base: release/10-4
        D,  # app module: detected
        "stagingDebug",  # variant, capitalised by the wizard
        "com.acme.app.staging",  # application id
        D,  # launcher: default launcher
        0,  # Jira
        "https://acme.atlassian.net/jira/software/projects/APP",
        "app, web",
        2,  # ceiling
        0,  # sign in with twg
        D,  # GitHub account: default = the one that can push
        D,  # coding agent: Claude Code
        False,  # no Slack
    )
    fake = Fake()
    res = Wizard(fresh, s, fake).run()
    lc = config.load(fresh)
    assert res.repo_written and res.todo == [] and res.secrets == []
    assert lc.cfg.project.base_branch == "release/10-4"
    assert lc.cfg.android.variant == "StagingDebug"
    assert lc.cfg.android.application_id == "com.acme.app.staging" and lc.cfg.android.app_module == "app"
    assert lc.cfg.tracker.site == "acme.atlassian.net" and lc.cfg.tracker.projects == ["APP", "WEB"]
    assert lc.cfg.autonomy.ceiling == 2
    assert lc.cfg.tracker.provider == "twg" and lc.cfg.vcs.github_account == "client"
    assert "client" not in config.config_path(fresh).read_text()  # the dev's account stays in local.yaml
    assert not secrets.exists()
    assert not (fresh / ".factory/local.yaml").exists()  # settings live outside the repo


def test_new_dev_on_existing_repo_with_api_token(repo: Path, secrets: Path) -> None:
    config.config_path(repo).write_text(
        "version: 1\nproject: {name: Demo App}\ntracker: {kind: jira, site: acme.atlassian.net}\n"
    )
    fake = Fake(tools={"gh"}, accounts=["work"], push={"work"})
    fake.rest_ok_after = 1  # first token is wrong
    s = Script(
        D,  # Jira sign-in: default is API token because twg is missing
        "dev@acme.com",
        "wrong",
        "dev@acme.com",
        "right-token",
        D,  # GitHub account
        1,  # coding agent: Cursor
        True,
        "https://hooks.slack.com/services/T/B/x",
    )
    res = Wizard(repo, s, fake).run()
    assert not res.repo_written
    assert local(repo)["agents"]["use"] == ["cursor"]
    assert res.secrets == ["JIRA_EMAIL__ACME", "JIRA_API_TOKEN__ACME", "SLACK_WEBHOOK_URL__DEMO_APP"]
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert "right-token" in secrets.read_text() and "right-token" not in config.local_path(repo).read_text()
    lc = config.load(repo)
    assert lc.cfg.tracker.provider == "rest" and lc.cfg.tracker.token == "right-token"
    assert lc.cfg.notifications.slack_webhook.startswith("https://hooks.slack.com/")
    assert any("rejected" in line for line in s.log)


def test_twg_installed_from_pasted_command(repo: Path, secrets: Path) -> None:
    config.config_path(repo).write_text(
        "version: 1\nproject: {name: d}\ntracker: {kind: jira, site: acme.atlassian.net}\n"
    )
    fake = Fake(tools={"gh"})
    s = Script(0, "curl -fsSL https://intranet.example/install-twg.sh | sh", D, D, False)
    res = Wizard(repo, s, fake).run()
    assert fake.ran == ["curl -fsSL https://intranet.example/install-twg.sh | sh"]
    assert local(repo)["setup"]["tools"]["twg"]["install"].startswith("curl")
    assert res.todo == []


def test_twg_login_retry_then_todo(repo: Path, secrets: Path) -> None:
    config.config_path(repo).write_text(
        "version: 1\nproject: {name: d}\ntracker: {kind: jira, site: acme.atlassian.net}\n"
    )
    fake = Fake()
    fake.twg_ok = False
    s = Script(0, True, False, D, D, False)  # twg; login once; then give up
    res = Wizard(repo, s, fake).run()
    assert fake.ran == ["twg --site acme login"]
    assert any("twg login" in t for t in res.todo)


def test_no_github_login_and_no_push_access(repo: Path, secrets: Path) -> None:
    fake = Fake(accounts=[])
    res = Wizard(repo, Script(False, D, False), fake).run()
    assert any("gh auth login" in t for t in res.todo)

    fake = Fake(accounts=["work"], push=set())
    res = Wizard(repo, Script(D, D, False), fake).run()
    assert any("write access" in t for t in res.todo)
    assert local(repo)["vcs"]["github_account"] == "work"


def test_flavored_app_must_name_its_variant_and_other_base(fresh: Path, secrets: Path) -> None:
    fake = Fake()
    fake.flavors = True
    s = Script(4, "hotfix/9", D, "", "prodAcmeDebug", "com.acme", ".Main", 1, 1, D, D, False)
    Wizard(fresh, s, fake).run()
    lc = config.load(fresh)
    assert lc.cfg.project.base_branch == "hotfix/9"
    assert lc.cfg.android.variant == "ProdAcmeDebug"  # empty answer was asked again
    assert lc.cfg.tracker.kind == "file"
    assert any("product flavors" in line for line in s.log)


def test_variant_pasted_from_android_studio_is_normalised() -> None:
    from mobile_factory.wizard import normalize_variant

    assert normalize_variant("superapp Stage JibestreamFcm Debug (default)") == "SuperappStageJibestreamFcmDebug"
    assert normalize_variant("stagingDebug") == "StagingDebug"


def test_variant_is_picked_from_gradle_list(fresh: Path, secrets: Path) -> None:
    fake = Fake()
    fake.found = {"StageAcmeDebug": "com.acme.stage", "ProdAcmeDebug": "com.acme"}
    Wizard(fresh, Script(0, D, 1, D, D, 1, 1, D, D, False), fake).run()
    android = config.load(fresh).cfg.android
    assert android.variant == "ProdAcmeDebug" and android.application_id == "com.acme"  # id defaulted from Gradle


def test_variant_other_than_gradle_list_is_typed(fresh: Path, secrets: Path) -> None:
    fake = Fake()
    fake.found, fake.flavors = {"StageAcmeDebug": "com.acme.stage"}, True
    Wizard(fresh, Script(0, D, 1, "qaAcmeDebug", "com.acme", D, 1, 1, D, D, False), fake).run()
    assert config.load(fresh).cfg.android.variant == "QaAcmeDebug"


def test_jira_keys_guessed_from_commits_and_branches(repo: Path) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "APP-1: fix")
    git(repo, "commit", "-q", "--allow-empty", "-m", "APP-2: fix WEB-3")
    git(repo, "branch", "feature/APP-4")
    assert Checks().jira_keys(repo) == ["APP", "WEB"]


def test_agent_default_follows_installed_cli(repo: Path, secrets: Path) -> None:
    fake = Fake(tools={"twg", "gh", "agent"})
    s = Script(0, D, D, False)  # twg, GitHub, agent: default, no Slack
    Wizard(repo, s, fake).run()
    assert local(repo)["agents"]["use"] == ["cursor"]


def _set_up_before(repo: Path) -> None:
    config.config_path(repo).write_text(
        "version: 1\nproject: {name: Demo, base_branch: release/10-4}\n"
        "tracker: {kind: jira, site: acme.atlassian.net, projects: [APP]}\nautonomy: {ceiling: 3}\n"
    )
    from mobile_factory.wizard import write_local

    write_local(
        repo, {"tracker": {"provider": "twg"}, "vcs": {"github_account": "client"}, "agents": {"use": ["claude"]}}
    )


def test_rerun_check_keeps_everything_with_one_keypress(repo: Path, secrets: Path) -> None:
    _set_up_before(repo)
    s = Script(D)  # "Check everything and keep my settings": nothing else is asked
    res = Wizard(repo, s, Fake()).run()
    assert not s.answers and not res.todo and not res.repo_written
    assert any("Already set up" in x for x in s.log) and any("ok  PRs as client" in x for x in s.log)
    assert local(repo)["vcs"]["github_account"] == "client"


def test_rerun_check_asks_only_what_broke(repo: Path, secrets: Path) -> None:
    _set_up_before(repo)
    fake = Fake(accounts=["work"], push={"work"})  # the saved account is gone from this machine
    s = Script(D, D)  # check; then the GitHub question (default: the account that can push)
    Wizard(repo, s, fake).run()
    assert local(repo)["vcs"]["github_account"] == "work" and any("client is not logged in" in x for x in s.log)


def test_rerun_change_some_asks_only_those_sections_from_saved_values(repo: Path, secrets: Path) -> None:
    _set_up_before(repo)
    fake = Fake(accounts=["work", "client"], push={"client", "work"})
    # change some: each question comes right before its section; GitHub y (keep its default), the rest n
    s = Script(1, False, False, True, D, False, False)
    Wizard(repo, s, fake).run()
    assert not s.answers and local(repo)["vcs"]["github_account"] == "client"  # default was the saved account


def test_rerun_start_over_defaults_to_current_values(repo: Path, secrets: Path) -> None:
    _set_up_before(repo)
    # start over: every project and personal question answered with its default
    s = Script(2, D, D, D, D, D, D, D, D, D, D, D, D, False)
    Wizard(repo, s, Fake()).run()
    cfg = config.load(repo).cfg
    assert cfg.project.base_branch == "release/10-4" and cfg.autonomy.ceiling == 3 and cfg.tracker.projects == ["APP"]

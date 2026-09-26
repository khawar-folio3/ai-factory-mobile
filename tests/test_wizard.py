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

    flavors = False


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "secrets.env"
    monkeypatch.setenv("FACTORY_SECRETS_FILE", str(f))
    return f


@pytest.fixture
def fresh(repo: Path) -> Path:
    (repo / "factory.yaml").unlink()
    git(repo, "commit", "-q", "-am", "no factory yet")
    return repo


def local(repo: Path) -> dict[str, Any]:
    return yaml.safe_load((repo / ".factory/local.yaml").read_text())


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
    assert "client" not in (fresh / "factory.yaml").read_text()  # the dev's account stays in local.yaml
    assert not secrets.exists()
    assert git(fresh, "check-ignore", ".factory/local.yaml") == ".factory/local.yaml"


def test_new_dev_on_existing_repo_with_api_token(repo: Path, secrets: Path) -> None:
    (repo / "factory.yaml").write_text(
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
        True,
        "https://hooks.slack.com/services/T/B/x",
    )
    res = Wizard(repo, s, fake).run()
    assert not res.repo_written
    assert res.secrets == ["JIRA_EMAIL__ACME", "JIRA_API_TOKEN__ACME", "SLACK_WEBHOOK_URL__DEMO_APP"]
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert "right-token" in secrets.read_text() and "right-token" not in (repo / ".factory/local.yaml").read_text()
    lc = config.load(repo)
    assert lc.cfg.tracker.provider == "rest" and lc.cfg.tracker.token == "right-token"
    assert lc.cfg.notifications.slack_webhook.startswith("https://hooks.slack.com/")
    assert any("rejected" in line for line in s.log)


def test_twg_installed_from_pasted_command(repo: Path, secrets: Path) -> None:
    (repo / "factory.yaml").write_text(
        "version: 1\nproject: {name: d}\ntracker: {kind: jira, site: acme.atlassian.net}\n"
    )
    fake = Fake(tools={"gh"})
    s = Script(0, "curl -fsSL https://intranet.example/install-twg.sh | sh", D, False)
    res = Wizard(repo, s, fake).run()
    assert fake.ran == ["curl -fsSL https://intranet.example/install-twg.sh | sh"]
    assert local(repo)["setup"]["tools"]["twg"]["install"].startswith("curl")
    assert res.todo == []


def test_twg_login_retry_then_todo(repo: Path, secrets: Path) -> None:
    (repo / "factory.yaml").write_text(
        "version: 1\nproject: {name: d}\ntracker: {kind: jira, site: acme.atlassian.net}\n"
    )
    fake = Fake()
    fake.twg_ok = False
    s = Script(0, True, False, D, False)  # twg; login once; then give up
    res = Wizard(repo, s, fake).run()
    assert fake.ran == ["twg --site acme login"]
    assert any("twg login" in t for t in res.todo)


def test_no_github_login_and_no_push_access(repo: Path, secrets: Path) -> None:
    fake = Fake(accounts=[])
    res = Wizard(repo, Script(False, False), fake).run()
    assert any("gh auth login" in t for t in res.todo)

    fake = Fake(accounts=["work"], push=set())
    res = Wizard(repo, Script(D, False), fake).run()
    assert any("write access" in t for t in res.todo)
    assert local(repo)["vcs"]["github_account"] == "work"


def test_flavored_app_must_name_its_variant_and_other_base(fresh: Path, secrets: Path) -> None:
    fake = Fake()
    fake.flavors = True
    s = Script(4, "hotfix/9", D, "", "prodAcmeDebug", "com.acme", ".Main", 1, 1, D, False)
    Wizard(fresh, s, fake).run()
    lc = config.load(fresh)
    assert lc.cfg.project.base_branch == "hotfix/9"
    assert lc.cfg.android.variant == "ProdAcmeDebug"  # empty answer was asked again
    assert lc.cfg.tracker.kind == "file"
    assert any("product flavors" in line for line in s.log)

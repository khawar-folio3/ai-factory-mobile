from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import config
from . import setup as machine
from .config import CONFIG_NAME, LOCAL_NAME, TrackerConfig
from .errors import FactoryError
from .init import init as write_repo_config
from .integrations import github
from .integrations.tracker import JiraRest, JiraTwg, normalize_site, site_prefix
from .proc import has, run

MAX_TRIES = 3


class Prompter(Protocol):
    def say(self, text: str) -> None: ...
    def ask(self, question: str, default: str = "") -> str: ...
    def secret(self, question: str) -> str: ...
    def choose(self, question: str, options: list[str], default: int = 0) -> int: ...
    def confirm(self, question: str, default: bool = True) -> bool: ...


@dataclass
class Checks:
    """Everything the wizard asks the outside world; replaced in tests."""

    has: Any = has
    shell: Any = machine.shell
    gh_accounts: Any = github.accounts
    gh_can_push: Any = github.can_push

    def twg_whoami(self, site: str) -> str:
        return JiraTwg(TrackerConfig(kind="jira", provider="twg", site=site)).ping()

    def jira_rest(self, site: str, email: str, token: str) -> str:
        return JiraRest(TrackerConfig(kind="jira", provider="rest", site=site, email=email, token=token)).ping()

    def figma_ready(self) -> bool:
        return machine._port_open(machine.FIGMA_MCP_PORT)

    def remote_branches(self, root: Path) -> list[str]:
        r = run(
            ["git", "for-each-ref", "--sort=-committerdate", "--format=%(refname:lstrip=3)", "refs/remotes/origin"],
            root,
        )
        return [b for b in r.out.split() if b != "HEAD"][:8]


@dataclass
class Result:
    repo_written: bool = False
    local: dict[str, Any] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)


def _env_name(prefix: str, scope: str) -> str:
    return f"{prefix}__{re.sub(r'[^A-Z0-9]+', '_', scope.upper()).strip('_')}"


def _write_local(root: Path, data: dict[str, Any]) -> None:
    f = root / LOCAL_NAME
    f.parent.mkdir(parents=True, exist_ok=True)
    merged = config.deep_merge(config.read_yaml(f), data)
    f.write_text(
        "# Per-developer factory settings (git-ignored). Written by `factory init`.\n"
        + yaml.safe_dump(merged, sort_keys=False)
    )
    if not run(["git", "check-ignore", "-q", LOCAL_NAME], root).ok:  # repo predates local.yaml: ignore it locally
        exclude = Path(run(["git", "rev-parse", "--git-path", "info/exclude"], root).out.strip())
        exclude = exclude if exclude.is_absolute() else root / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as fh:
            fh.write(f"/{LOCAL_NAME}\n")


def store_secret(name: str, value: str, secrets_file: str = config.DEFAULT_SECRETS_FILE) -> Path:
    path = config.secrets_path(secrets_file)
    current = config.read_env_file(path)
    current[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in sorted(current.items())))
    os.chmod(path, 0o600)
    return path


class Wizard:
    def __init__(self, root: Path, p: Prompter, checks: Checks | None = None) -> None:
        self.root = root
        self.p = p
        self.c = checks or Checks()
        self.result = Result()

    # ---------- repo (once, by the first person) ----------

    def repo(self) -> None:
        p = self.p
        p.say("\n== Project settings (shared, committed in factory.yaml)")
        branches = self.c.remote_branches(self.root)
        options = [*branches, "ask on every run"]
        base = options[p.choose("Base branch for fixes", options, 0)] if branches else p.ask("Base branch", "main")
        answers = {"__BASE__": "ask" if base == "ask on every run" else base}
        answers["__VARIANT__"] = p.ask("Gradle debug variant suffix (install<Variant>)", "Debug")

        kind = p.choose("Where do tickets live?", ["Jira", "Markdown files in .factory/tickets (no Jira)"], 0)
        if kind == 0:
            site = ""
            while not site:
                site = normalize_site(p.ask("Jira site URL (e.g. https://acme.atlassian.net)"))
            keys = [
                k.strip().upper()
                for k in p.ask("Jira project keys, comma separated (empty = any)").split(",")
                if k.strip()
            ]
            answers |= {"__TRACKER_KIND__": "jira", "__JIRA_SITE__": site, "__PROJECTS__": json.dumps(keys)}
        else:
            answers |= {"__TRACKER_KIND__": "file", "__JIRA_SITE__": ""}
        levels = [
            "0 manual: every gate asks",
            "1 assisted",
            "2 supervised",
            "3 trusted: only the PR asks",
            "4 autonomous",
        ]
        answers["__CEILING__"] = str(p.choose("Autonomy ceiling for this repo", levels, 1))
        for line in write_repo_config(self.root, force=(self.root / CONFIG_NAME).exists(), answers=answers):
            p.say(f"  {line}")
        self.result.repo_written = True

    # ---------- developer (every person, every machine) ----------

    def jira(self, lc: config.LoadedConfig) -> None:
        t = lc.cfg.tracker
        if t.kind != "jira":
            return
        p = self.p
        p.say(f"\n== Jira ({t.site})")
        choice = p.choose(
            "How do you sign in to Jira?",
            [
                "twg CLI (your Atlassian login, recommended)",
                "Jira API token (id.atlassian.com → Security → API tokens)",
            ],
            0 if self.c.has("twg") else 1,
        )
        if choice == 0:
            self._twg(t.site)
        else:
            self._jira_token(t.site)

    def _twg(self, site: str) -> None:
        p = self.p
        if not self.c.has("twg"):
            cmd = p.ask("twg is not installed. Paste the install command your team uses (empty to skip)")
            if not cmd:
                self.result.todo.append("install twg, then re-run `factory init`")
                return
            if self.c.shell(cmd) != 0 or not self.c.has("twg"):
                self.result.todo.append(f"twg install failed: {cmd}")
                return
            self.result.local = config.deep_merge(self.result.local, {"setup": {"tools": {"twg": {"install": cmd}}}})
        for _ in range(MAX_TRIES):
            try:
                who = self.c.twg_whoami(site)
                p.say(f"  ok  {who}")
                self.result.local = config.deep_merge(self.result.local, {"tracker": {"provider": "twg"}})
                return
            except FactoryError as e:
                p.say(f"  twg cannot reach {site}: {str(e)[:160]}")
                if not p.confirm("Run `twg login` now?"):
                    break
                self.c.shell(f"twg --site {site_prefix(site)} login")
        self.result.todo.append(f"twg cannot reach {site}: run `twg login`, then `factory init` again")

    def _jira_token(self, site: str) -> None:
        p = self.p
        email_var, token_var = (
            _env_name("JIRA_EMAIL", site_prefix(site)),
            _env_name("JIRA_API_TOKEN", site_prefix(site)),
        )
        for _ in range(MAX_TRIES):
            email = p.ask("Atlassian account email")
            token = p.secret("Jira API token (hidden)")
            try:
                who = self.c.jira_rest(site, email, token)
            except FactoryError as e:
                p.say(f"  rejected: {str(e)[:160]}")
                continue
            store_secret(email_var, email)
            store_secret(token_var, token)
            self.result.secrets += [email_var, token_var]
            self.result.local = config.deep_merge(
                self.result.local,
                {"tracker": {"provider": "rest", "email": f"${{{email_var}}}", "token": f"${{{token_var}}}"}},
            )
            p.say(f"  ok  {who}")
            return
        self.result.todo.append("Jira API token not verified: re-run `factory init`")

    def github(self, lc: config.LoadedConfig) -> None:
        p = self.p
        try:
            repo = run(["git", "remote", "get-url", lc.cfg.vcs.remote], self.root).out.strip()
        except FactoryError:
            repo = ""
        slug = re.sub(r"\.git$", "", re.sub(r"^(git@|ssh://git@|https://)[^/:]+[:/]", "", repo))
        p.say(f"\n== GitHub ({slug or 'no remote'})")
        accounts = self.c.gh_accounts()
        if not accounts:
            p.say("  no GitHub login on this machine")
            if p.confirm("Run `gh auth login --web` now?"):
                self.c.shell("gh auth login --hostname github.com --git-protocol https --web")
                accounts = self.c.gh_accounts()
        if not accounts:
            self.result.todo.append("gh auth login --web, then `factory init` again")
            return
        pushable = [a for a in accounts if slug and self.c.gh_can_push(slug, a)]
        default = accounts.index(pushable[0]) if pushable else 0
        labels = [f"{a}{'  (can push)' if a in pushable else ''}" for a in accounts]
        account = accounts[p.choose("Which GitHub account opens PRs for this repo?", labels, default)]
        if slug and account not in pushable:
            p.say(f"  warning: {account} cannot push to {slug}; PRs will fail until it has write access")
            self.result.todo.append(f"give {account} write access to {slug}")
        self.result.local = config.deep_merge(self.result.local, {"vcs": {"github_account": account}})
        p.say(f"  ok  PRs as {account}")

    def slack(self, lc: config.LoadedConfig) -> None:
        p = self.p
        if not p.confirm("\nPost gate/finish notifications to Slack?", default=False):
            return
        var = _env_name("SLACK_WEBHOOK_URL", lc.cfg.project.name)
        url = p.secret("Slack incoming webhook URL (hidden)")
        if not url.startswith("https://hooks.slack.com/"):
            p.say("  not a Slack webhook URL, skipped")
            return
        store_secret(var, url, lc.cfg.secrets_file)
        self.result.secrets.append(var)
        self.result.local = config.deep_merge(self.result.local, {"notifications": {"slack_webhook": f"${{{var}}}"}})

    def figma(self, lc: config.LoadedConfig) -> None:
        p = self.p
        mode = config.figma_mode(lc.cfg)
        if mode == "none":
            return
        p.say(f"\n== Figma ({mode} MCP server)")
        if mode == "remote":
            p.say("  your agent asks you to sign in to Figma in the browser the first time it reads a design")
            p.say("  real use needs a Dev or Full seat on a paid Figma plan (Starter: 20 calls/month)")
            return
        if self.c.figma_ready():
            p.say("  ok  desktop MCP server is running")
            return
        p.say(f"  {machine.DESKTOP_HINT}")
        if p.confirm("Done? (check again)") and self.c.figma_ready():
            p.say("  ok")
            return
        self.result.todo.append("enable Figma's desktop MCP server")

    # ---------- flow ----------

    def run(self, reconfigure: bool = False) -> Result:
        p = self.p
        existing = (self.root / CONFIG_NAME).exists()
        p.say(f"Mobile Factory setup for {self.root.name}")
        if existing and not reconfigure:
            p.say("factory.yaml found: this repo is set up; configuring you on this machine.")
        else:
            self.repo()
        lc = config.load(self.root)
        self.jira(lc)
        self.github(lc)
        self.slack(lc)
        self.figma(lc)
        if self.result.local:
            _write_local(self.root, self.result.local)
            p.say(f"\nsaved your settings to {LOCAL_NAME} (git-ignored)")
        if self.result.secrets:
            p.say(f"saved secrets {', '.join(self.result.secrets)} to {config.secrets_path(lc.cfg.secrets_file)}")
        return self.result

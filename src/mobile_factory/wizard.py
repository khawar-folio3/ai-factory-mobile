from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import config
from . import setup as machine
from .config import TrackerConfig
from .errors import FactoryError
from .init import detect
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
        """Likely PR bases first: GitHub default branch, newest release branches, then develop/main/master."""
        r = run(
            ["git", "for-each-ref", "--sort=-committerdate", "--format=%(refname:lstrip=3)", "refs/remotes/origin"],
            root,
        )
        refs = [b for b in r.out.split() if b != "HEAD"]
        d = run(["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"], root)
        default = [d.out.strip()] if d.ok and d.out.strip() else []
        releases = [b for b in refs if re.match(r"^release/v?\d", b)][:4]
        trunks = [b for b in ("develop", "main", "master") if b in refs]
        return list(dict.fromkeys([*default, *releases, *trunks]))

    def jira_keys(self, root: Path) -> list[str]:
        """Most-used Jira keys in recent commit subjects and branch names, e.g. ['SCPB', 'CA']."""
        log = run(["git", "log", "-300", "--format=%s"], root).out
        branches = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"], root).out
        found = Counter(re.findall(r"\b([A-Z][A-Z0-9]{1,9})-\d+", log + branches))
        return [k for k, _ in found.most_common(3)]

    def variants(self, root: Path, module: str) -> dict[str, str]:
        """{variant: application id} for every installable variant, debug first; {} when Gradle can't answer."""
        if not (root / "gradlew").is_file():
            return {}
        with resources.as_file(resources.files("mobile_factory.templates") / "variants.gradle") as script:
            out = _gradle_out(root, "-I", str(script), f":{module}:factoryVariants")
        found = {n[:1].upper() + n[1:]: i for n, i in re.findall(r"^factory-variant (\w+) (\S+)$", out, re.M)}
        if not found:  # AGP < 7 has no androidComponents: names only
            out = _gradle_out(root, f":{module}:tasks", "--group", "install")
            found = dict.fromkeys(re.findall(r"^install(\w+) - Installs the \w+ build", out, re.M), "")
        return dict(sorted(found.items(), key=lambda kv: not kv[0].endswith("Debug")))

    def has_flavors(self, root: Path) -> bool:
        files = [*root.glob("*.gradle*"), *root.glob("*/build.gradle*")]
        return any(
            re.search(r"productFlavors|flavorDimensions", f.read_text(errors="ignore")) for f in files if f.is_file()
        )


@dataclass
class Result:
    repo_written: bool = False
    local: dict[str, Any] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)


def _gradle_out(root: Path, *args: str) -> str:
    try:
        r = run([str(root / "gradlew"), "-q", *args], root, timeout=600)
    except FactoryError:
        return ""
    return r.out if r.ok else ""


def _index(options: list[str], value: object) -> int:
    return options.index(value) if isinstance(value, str) and value in options else 0


def normalize_variant(value: str) -> str:
    """Android Studio shows 'superapp Stage Acme Debug (default)'; Gradle wants 'SuperappStageAcmeDebug'."""
    v = re.sub(r"\s+", "", re.sub(r"\(.*?\)", "", value))
    return v[:1].upper() + v[1:]


def _env_name(prefix: str, scope: str) -> str:
    return f"{prefix}__{re.sub(r'[^A-Z0-9]+', '_', scope.upper()).strip('_')}"


def write_local(root: Path, data: dict[str, Any]) -> Path:
    f = config.local_path(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    merged = config.deep_merge(config.read_yaml(f), data)
    f.write_text(
        f"# Your factory settings for {root.name} (outside the repo). Written by `factory init`.\n"
        + yaml.safe_dump(merged, sort_keys=False)
    )
    return f


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
        self.saved: dict[str, Any] = {}

    # ---------- repo (once, by the first person) ----------

    def repo(self, cur: config.FactoryConfig | None = None) -> None:
        """Project questions; with `cur` (already set up) every question starts from the saved value."""
        p = self.p
        p.say("\n== Project settings")
        branches = self.c.remote_branches(self.root)
        saved_base = cur.project.base_branch if cur else ""
        if saved_base and saved_base != "ask" and saved_base not in branches:
            branches = [saved_base, *branches]
        options = [*branches, "ask on every run", "other (type it)"]
        base_default = options.index("ask on every run") if saved_base == "ask" else _index(options, saved_base)
        pick = options[p.choose("Base branch PRs target", options, base_default)]
        if pick == "other (type it)":
            pick = p.ask("Base branch", branches[0] if branches else "main")
        answers = {"__BASE__": "ask" if pick == "ask on every run" else pick}

        detected = detect(self.root)
        answers["__APP_MODULE__"] = p.ask(
            "App module (the one that builds the APK)", cur.android.app_module if cur else detected["__APP_MODULE__"]
        )
        p.say("  reading build variants from Gradle…")
        found = self.c.variants(self.root, answers["__APP_MODULE__"])
        variant = ""
        if found:
            names = list(found)
            i = p.choose(
                "Variant to build and install", [*names, "other (type it)"], _index(names, cur and cur.android.variant)
            )
            variant = names[i] if i < len(names) else ""
        if not variant and self.c.has_flavors(self.root):
            p.say("  this app has product flavors: the variant is <flavors><BuildType>, e.g. stageAcmeDebug")
            p.say("  (Android Studio → Build Variants panel shows the exact name)")
            while not variant:
                variant = p.ask("Variant to build and install")
        elif not variant:
            variant = p.ask("Variant to build and install", "Debug")
        answers["__VARIANT__"] = normalize_variant(variant)
        answers["__APP_ID__"] = p.ask(
            "Application id of that build on the device (e.g. com.acme.app.debug)",
            found.get(answers["__VARIANT__"]) or (cur and cur.android.application_id) or detected["__APP_ID__"],
        )
        answers["__LAUNCHER__"] = p.ask(
            "Launcher activity, fully qualified (empty = the app's default launcher)",
            (cur and cur.android.launch_activity) or detected["__LAUNCHER__"],
        )

        tickets = ["Jira", "Markdown ticket files in the factory home (no Jira)"]
        kind = p.choose("Where do tickets live?", tickets, 1 if cur and cur.tracker.kind == "file" else 0)
        if kind == 0:
            site = ""
            while not site:
                site = normalize_site(
                    p.ask(
                        "Jira site URL (e.g. https://acme.atlassian.net)",
                        (cur and cur.tracker.site) or detected["__JIRA_SITE__"],
                    )
                )
            keys_default = (
                (", ".join(cur.tracker.projects) or "any") if cur else ", ".join(self.c.jira_keys(self.root)) or "any"
            )
            raw = p.ask("Jira project keys, comma separated (any = every project)", keys_default)
            keys = [] if raw.strip().lower() == "any" else [k.strip().upper() for k in raw.split(",") if k.strip()]
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
        answers["__CEILING__"] = str(
            p.choose("Autonomy ceiling for this repo", levels, cur.autonomy.ceiling if cur else 1)
        )
        for line in write_repo_config(self.root, force=config.config_path(self.root).exists(), answers=answers):
            p.say(f"  {line}")
        self.result.repo_written = True

    # ---------- developer (every person, every machine) ----------

    def jira(self, lc: config.LoadedConfig, check: bool = False) -> None:
        t = lc.cfg.tracker
        if t.kind != "jira":
            return
        p = self.p
        p.say(f"\n== Jira ({t.site})")
        saved = (self.saved.get("tracker") or {}).get("provider")
        if check and saved:
            try:
                who = self.c.twg_whoami(t.site) if saved == "twg" else self.c.jira_rest(t.site, t.email, t.token)
                p.say(f"  ok  {who}")
                return
            except FactoryError as e:
                p.say(f"  warning: your saved Jira sign-in stopped working: {str(e)[:120]}")
        default = {"twg": 0, "rest": 1}.get(saved or "", 0 if self.c.has("twg") else 1)
        choice = p.choose(
            "How do you sign in to Jira?",
            [
                "twg CLI (your Atlassian login, recommended)",
                "Jira API token (id.atlassian.com → Security → API tokens)",
            ],
            default,
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
            email = p.ask("Atlassian account email", run(["git", "config", "user.email"], self.root).out.strip())
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

    def github(self, lc: config.LoadedConfig, check: bool = False) -> None:
        p = self.p
        saved = (self.saved.get("vcs") or {}).get("github_account", "")
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
        if check and saved in accounts and (not slug or saved in pushable):
            p.say(f"  ok  PRs as {saved}")
            return
        if check and saved:
            p.say(f"  warning: {saved} is {'not logged in' if saved not in accounts else 'unable to push'} here")
        default = accounts.index(saved) if saved in accounts else accounts.index(pushable[0]) if pushable else 0
        labels = [f"{a}{'  (can push)' if a in pushable else ''}" for a in accounts]
        account = accounts[p.choose("Which GitHub account opens PRs for this repo?", labels, default)]
        if slug and account not in pushable:
            p.say(f"  warning: {account} cannot push to {slug}; PRs will fail until it has write access")
            self.result.todo.append(f"give {account} write access to {slug}")
        self.result.local = config.deep_merge(self.result.local, {"vcs": {"github_account": account}})
        p.say(f"  ok  PRs as {account}")

    def agent(self, check: bool = False) -> None:
        p = self.p
        p.say("\n== Coding agent")
        claude, cursor = self.c.has("claude"), self.c.has("agent") or self.c.has("cursor-agent")
        saved = (self.saved.get("agents") or {}).get("use") or []
        if check and saved:
            p.say(f"  ok  agent: {' + '.join(saved)}  (Machine tools checks its CLI and login)")
            return
        p.say("  runs the agent steps (triage, fix, review, taste distill); Machine tools installs and logs in its CLI")
        options = [
            f"Claude Code{'  (installed)' if claude else ''}",
            f"Cursor{'  (installed)' if cursor else ''}",
            "Both",
        ]
        choices = [["claude"], ["cursor"], ["claude", "cursor"]]
        default = choices.index(saved) if saved in choices else 2 if claude and cursor else 1 if cursor else 0
        use = choices[p.choose("Which coding agent do you use?", options, default)]
        self.result.local = config.deep_merge(self.result.local, {"agents": {"use": use}})
        p.say(f"  ok  agent: {' + '.join(use)}")

    def slack(self, lc: config.LoadedConfig, check: bool = False) -> None:
        p = self.p
        on = bool((self.saved.get("notifications") or {}).get("slack_webhook"))
        if check:
            p.say(f"\n  ok  Slack notifications {'on' if on else 'off'}")
            return
        if not p.confirm("\nPost gate/finish notifications to Slack?", default=on):
            if on:
                self.result.local = config.deep_merge(self.result.local, {"notifications": {"slack_webhook": ""}})
            return
        var = _env_name("SLACK_WEBHOOK_URL", lc.cfg.project.name)
        url = p.secret("Slack incoming webhook URL (hidden)")
        if not url.startswith("https://hooks.slack.com/"):
            p.say("  not a Slack webhook URL, skipped")
            return
        store_secret(var, url, lc.cfg.secrets_file)
        self.result.secrets.append(var)
        self.result.local = config.deep_merge(self.result.local, {"notifications": {"slack_webhook": f"${{{var}}}"}})

    def figma(self, lc: config.LoadedConfig, check: bool = False) -> None:
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

    def _summary(self, lc: config.LoadedConfig) -> str:
        """Every saved setting, in full, one aligned row each (`  · Key  value`: the terminal styles the key)."""
        return "\n== Already set up\n" + "\n".join(f"  · {k}  {v}" for k, v in self._rows(lc))

    def _rows(self, lc: config.LoadedConfig) -> list[tuple[str, str]]:
        c = lc.cfg
        levels = ["manual", "assisted", "supervised", "trusted", "autonomous"]
        signin = {"twg": "twg (your Atlassian login)", "rest": "Jira API token"}.get(
            c.tracker.provider, c.tracker.provider
        )
        agents = {"claude": "Claude Code", "cursor": "Cursor"}
        taste = lc.path(c.guardrail.taste)
        rules = sum(1 for ln in taste.read_text().splitlines() if ln.startswith("### R")) if taste.is_file() else 0
        rows = [
            ("Base branch", c.project.base_branch),
            ("App module", c.android.app_module),
            ("Variant", c.android.variant),
            ("App id", c.android.application_id or "not set"),
            ("Launcher", c.android.launch_activity or "the app's default launcher"),
        ]
        if c.tracker.kind == "jira":
            rows += [
                ("Jira", c.tracker.site),
                ("Projects", ", ".join(c.tracker.projects) or "any"),
                ("Sign-in", signin),
            ]
        else:
            rows.append(("Tickets", f"files in {lc.path('tickets')}"))
        rows += [
            ("Autonomy", f"{c.autonomy.ceiling} · {levels[c.autonomy.ceiling]}"),
            ("GitHub", c.vcs.github_account or "gh's active account"),
            ("Agent", " + ".join(agents.get(a, a) for a in c.agents.use)),
            ("Taste rules", f"{rules} rules" if rules else "none yet"),
            ("Slack", "on" if c.notifications.slack_webhook else "off"),
            (
                "Pixel office",
                ("on · live hooks " + ("on" if c.viz.pixel_hooks else "off")) if c.viz.pixel_agents else "off",
            ),
            ("Stored in", str(lc.state_dir).replace(str(Path.home()), "~")),
        ]
        return rows

    def run(self, reconfigure: bool = False) -> Result:
        """First time: every question. Already set up: check the saved settings, change some, or start over."""
        p = self.p
        existing = config.config_path(self.root).exists()
        self.saved = config.read_yaml(config.local_path(self.root))
        p.say(f"Mobile Factory setup for {self.root.name}")
        sections = ["Project", "Jira", "GitHub", "Coding agent", "Slack", "Figma"]
        change: set[str] = set(sections)  # sections to ask; the rest are only checked
        pick_each = False  # "Change some settings": ask per section, right before it
        if existing and self.saved and not reconfigure:
            p.say(self._summary(config.load(self.root)))
            pick = p.choose(
                "What do you want to do?",
                ["Check everything and keep my settings", "Change some settings", "Start over"],
                0,
            )
            change = set(sections) if pick == 2 else set()
            pick_each = pick == 1
        elif existing and not reconfigure:
            change.discard("Project")  # set up, but not by you on this machine yet: your part only

        def ask(section: str, label: str, keys: tuple[str, ...]) -> bool:
            """Per section: its current settings, one row each, then "Change …?" right before the section."""
            if not pick_each:
                return section in change
            p.say("\n" + "\n".join(f"  · {k}  {v}" for k, v in self._rows(config.load(self.root)) if k in keys))
            return p.confirm(f"Change {label}?", False)

        lc = config.load(self.root) if existing else None
        project = (
            "Base branch",
            "App module",
            "Variant",
            "App id",
            "Launcher",
            "Jira",
            "Projects",
            "Tickets",
            "Autonomy",
        )
        if (lc and ask("Project", "project settings", project)) or (not lc and "Project" in change):
            self.repo(lc.cfg if lc else None)
        lc = config.load(self.root)
        c = lc.cfg
        jira_on = c.tracker.kind == "jira"
        self.jira(lc, check=not (jira_on and ask("Jira", "Jira sign-in", ("Sign-in",))))
        self.github(lc, check=not ask("GitHub", "GitHub account", ("GitHub",)))
        self.agent(check=not ask("Coding agent", "coding agent", ("Agent",)))
        self.slack(lc, check=not ask("Slack", "Slack notifications", ("Slack",)))
        figma = config.figma_mode(c)
        self.figma(lc, check=not (figma != "none" and ask("Figma", "Figma", ())))
        if self.result.local:
            where = str(write_local(self.root, self.result.local)).replace(str(Path.home()), "~")
            p.say(f"\n  ok  settings saved  ({where}: outside the repo, never committed)")
        if self.result.secrets:
            p.say(f"saved secrets {', '.join(self.result.secrets)} to {config.secrets_path(lc.cfg.secrets_file)}")
        return self.result

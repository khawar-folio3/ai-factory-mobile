from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..errors import FactoryError
from ..proc import run


def gh(root: Path, *args: str, check: bool = True) -> str:
    r = run(["gh", *args], root)
    if check and not r.ok:
        raise FactoryError(f"gh {' '.join(args[:3])} failed: {(r.err or r.out).strip()[:400]}")
    return r.out.strip()


def gh_json(root: Path, *args: str) -> Any:
    text = gh(root, *args)
    return json.loads(text) if text else None


def accounts() -> list[str]:
    r = run(["gh", "auth", "status", "--json", "hosts"])
    if not r.ok and not r.out:
        return []
    hosts = json.loads(r.out or "{}").get("hosts", {})
    return [a["login"] for a in hosts.get("github.com", []) if a.get("state") == "success"]


def account_env(account: str) -> dict[str, str]:
    """Env that makes gh and git act as `account` for this process only (active gh account and SSH keys untouched).
    SSH remotes are rewritten to HTTPS so the account's gh login, not whichever SSH key loads first, is used."""
    if not account:
        return {}
    r = run(["gh", "auth", "token", "--user", account])
    if not r.ok:
        raise FactoryError(f"gh has no login for {account}: gh auth login --web (as {account})")
    helper = "credential.https://github.com.helper"
    return {
        "GH_TOKEN": r.out.strip(),
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": helper,
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": helper,
        "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
        "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_2": "git@github.com:",
        "GIT_CONFIG_KEY_3": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_3": "ssh://git@github.com/",
    }


def activate(account: str) -> None:
    os.environ.update(account_env(account))


def can_push(repo: str, account: str) -> bool:
    env = {**os.environ, **account_env(account)}
    r = run(["gh", "api", f"repos/{repo}", "--jq", ".permissions.push"], env=env)
    return r.ok and r.out.strip() == "true"


def authenticated(root: Path) -> bool:
    return run(["gh", "auth", "status"], root).ok


def login(root: Path) -> str:
    r = run(["gh", "api", "user", "--jq", ".login"], root)
    return r.out.strip() if r.ok else ""


def push(root: Path, remote: str, branch: str) -> None:
    r = run(["git", "push", "--quiet", "-u", remote, f"HEAD:refs/heads/{branch}"], root)
    if not r.ok:
        raise FactoryError(f"git push failed: {r.err.strip()[:400]}")


def create_pr(root: Path, base: str, branch: str, title: str, body_file: Path, *, draft: bool, label: str) -> str:
    args = ["pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(body_file)]
    if draft:
        args.append("--draft")
    if label:
        args += ["--label", label]
    return gh(root, *args).splitlines()[-1]

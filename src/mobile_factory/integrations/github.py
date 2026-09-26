from __future__ import annotations

import json
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


def authenticated(root: Path) -> bool:
    return run(["gh", "auth", "status"], root).ok


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

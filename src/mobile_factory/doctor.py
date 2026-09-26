from __future__ import annotations

import stat
import sys

from .config import LoadedConfig, env_refs, secrets_path
from .errors import FactoryError
from .integrations import github, tracker
from .platforms import make as make_platform
from .platforms.base import Check
from .proc import has, run


def checks(lc: LoadedConfig, online: bool = True) -> list[Check]:
    c = lc.cfg
    out = [Check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])]
    out += [Check(f"{t} on PATH", has(t), "" if has(t) else f"install {t}") for t in ("git", "gh")]
    if c.tracker.kind == "jira" and c.tracker.provider == "twg":
        out.append(Check("twg on PATH", has("twg"), ""))

    sp = secrets_path(c.secrets_file)
    if sp.exists():
        mode = stat.S_IMODE(sp.stat().st_mode)
        out.append(Check("secrets file private", mode & 0o077 == 0, f"{sp} mode {oct(mode)}; chmod 600 {sp}"))
    missing = sorted(lc.missing_env)
    out.append(
        Check(
            "secrets resolved",
            not missing,
            f"{len(env_refs(lc.raw))} referenced"
            if not missing
            else f"missing: {', '.join(missing)} -> factory secrets set <NAME>",
        )
    )

    gitignore = (lc.root / ".gitignore").read_text() if (lc.root / ".gitignore").is_file() else ""
    ignored = ".factory/runs" in gitignore or ".factory/" in gitignore
    out.append(
        Check(".factory/runs ignored by git", ignored, "" if ignored else "add .factory/runs/ and .factory/data/")
    )
    has_taste = (lc.root / c.guardrail.taste).is_file()
    hint = "none yet: `factory guardrail learn`; until then only detectors and knowledge apply"
    out.append(Check("taste rules", has_taste, "" if has_taste else hint, optional=True))

    if online:
        out.append(Check("gh authenticated", has("gh") and github.authenticated(lc.root), "gh auth login"))
        if c.project.base_branch not in ("", "ask"):
            ok = run(["git", "ls-remote", "--exit-code", "--heads", c.vcs.remote, c.project.base_branch], lc.root).ok
            out.append(Check(f"base branch {c.project.base_branch}", ok, f"not found on {c.vcs.remote}"))
        if c.tracker.kind == "jira" and not missing:
            try:
                out.append(Check("tracker reachable", True, tracker.make(c.tracker, lc.root).ping()))
            except FactoryError as e:
                out.append(Check("tracker reachable", False, str(e)))
    out += make_platform(lc).doctor()
    return out


def render(results: list[Check]) -> tuple[str, bool]:
    def mark(r: Check) -> str:
        return "ok  " if r.ok else ("warn" if r.optional else "FAIL")

    lines = [f"{mark(r)}  {r.name}" + (f"  ({r.detail})" if r.detail else "") for r in results]
    ok = all(r.ok or r.optional for r in results)
    lines.append("DOCTOR: PASS" if ok else "DOCTOR: FAIL")
    return "\n".join(lines), ok

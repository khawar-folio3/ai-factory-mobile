from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from .config import CONFIG_NAME
from .errors import FactoryError
from .proc import has, run

TEMPLATE_DEFAULTS = {"__VARIANT__": "Debug", "__CEILING__": "1", "__PROJECTS__": "[]"}
GITIGNORE = [".factory/runs/", ".factory/data/", ".factory/events.jsonl", ".factory/evals/work/", ".factory/local.yaml"]


def _grep(files: list[Path], pattern: str) -> str:
    rx = re.compile(pattern)
    for f in files:
        if f.is_file() and (m := rx.search(f.read_text(errors="ignore"))):
            return m.group(1)
    return ""


def detect(root: Path) -> dict[str, str]:
    settings = [root / "settings.gradle.kts", root / "settings.gradle"]
    text = "\n".join(f.read_text(errors="ignore") for f in settings if f.is_file())
    modules = [m.lstrip(":").replace(":", "/") for m in re.findall(r"""["']:([\w:.-]+)["']""", text)] or ["app"]
    app = next(
        (
            m
            for m in modules
            if _grep([root / m / "build.gradle.kts", root / m / "build.gradle"], r"(android[.-]application)")
        ),
        "app",
    )
    app_id = _grep(
        [root / app / "build.gradle.kts", root / app / "build.gradle"], r"""applicationId\s*=?\s*["']([\w.]+)["']"""
    )
    manifest = root / app / "src/main/AndroidManifest.xml"
    launcher = ""
    if manifest.is_file():
        xml = manifest.read_text(errors="ignore")
        for block in re.findall(r"<activity\b(?:[^>]*/>|.*?</activity>)", xml, re.S):
            if "android.intent.category.LAUNCHER" in block and (m := re.search(r'android:name="([^"]+)"', block)):
                launcher = m.group(1)
                break
    kind, site = "file", ""
    if has("twg"):
        kind = "jira"
        conf = Path.home() / ".config" / "twg" / "auth.conf"
        site = _grep([conf], r"""site\s*[=:]\s*["']?([\w.-]+)""") if conf.is_file() else ""
        if site and "." not in site:
            site = f"{site}.atlassian.net"
    base = ""
    r = run(["gh", "repo", "view", "--json", "defaultBranchRef"], root)
    if r.ok:
        base = json.loads(r.out)["defaultBranchRef"]["name"]
    return {
        "__NAME__": root.name,
        "__BASE__": base or "ask",
        "__APP_MODULE__": app,
        "__MODULES__": json.dumps(modules),
        "__APP_ID__": app_id,
        "__LAUNCHER__": launcher,
        "__TRACKER_KIND__": kind,
        "__JIRA_SITE__": site,
    }


def init(root: Path, force: bool = False, answers: dict[str, str] | None = None) -> list[str]:
    cfg = root / CONFIG_NAME
    if cfg.exists() and not force:
        raise FactoryError(f"{cfg} exists (use --force to overwrite)")
    text = resources.files("mobile_factory.templates").joinpath("factory.yaml").read_text()
    detected = detect(root)
    values = {**TEMPLATE_DEFAULTS, **detected, **(answers or {})}
    for k, v in values.items():
        text = text.replace(k, v)
    cfg.write_text(text)
    for d in ("knowledge", "flows", "tickets"):
        (root / ".factory" / d).mkdir(parents=True, exist_ok=True)
    example = root / ".factory" / "knowledge" / "README.md"
    if not example.exists():
        example.write_text(resources.files("mobile_factory.templates").joinpath("knowledge-example.md").read_text())
    gi = root / ".gitignore"
    current = gi.read_text() if gi.is_file() else ""
    add = [ln for ln in GITIGNORE if ln not in current]
    if add:
        gi.write_text(
            current.rstrip() + ("\n\n" if current.strip() else "") + "# mobile-factory\n" + "\n".join(add) + "\n"
        )
    return [f"{k.strip('_').lower()}: {values[k] or '(not detected, fill in)'}" for k in detected]

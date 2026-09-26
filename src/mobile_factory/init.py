from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from . import config
from .config import CONFIG_NAME
from .errors import FactoryError
from .proc import has, run

TEMPLATE_DEFAULTS = {"__VARIANT__": "Debug", "__CEILING__": "1", "__PROJECTS__": "[]"}
# written into .gitignore by older versions; only `factory uninstall` still looks for them, to clean them up
GITIGNORE = [".factory/runs/", ".factory/data/", ".factory/events.jsonl", ".factory/evals/work/", ".factory/local.yaml"]


def _grep(files: list[Path], pattern: str) -> str:
    rx = re.compile(pattern)
    for f in files:
        if f.is_file() and (m := rx.search(f.read_text(errors="ignore"))):
            return m.group(1)
    return ""


def _launcher(module: Path) -> str:
    """Launcher activity from any of the module's manifests (src/main first; custom source sets too)."""
    main = module / "src/main/AndroidManifest.xml"
    manifests = [main, *sorted(m for m in module.glob("src/**/AndroidManifest.xml") if m != main)]
    for manifest in (m for m in manifests if m.is_file()):
        xml = re.sub(r"<!--.*?-->", "", manifest.read_text(errors="ignore"), flags=re.S)
        for block in re.findall(r"<activity\b(?:[^>]*/>|.*?</activity>)", xml, re.S):
            if "android.intent.category.LAUNCHER" in block and (m := re.search(r'android:name="([^"]+)"', block)):
                name = m.group(1)
                if name.startswith("."):
                    pkg = _grep([manifest], r'package="([\w.]+)"') or _grep(
                        [module / "build.gradle.kts", module / "build.gradle"], r"""namespace\s*=?\s*["']([\w.]+)["']"""
                    )
                    name = pkg + name
                return name
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
    launcher = _launcher(root / app)
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
    """Writes the repo's factory config into the per-developer home: the repo itself is never touched."""
    home = config.state_dir(root)
    cfg = home / CONFIG_NAME
    if cfg.exists() and not force:
        raise FactoryError(f"{cfg} exists (use --force to overwrite)")
    text = resources.files("mobile_factory.templates").joinpath("factory.yaml").read_text()
    detected = detect(root)
    values = {**TEMPLATE_DEFAULTS, **detected, **(answers or {})}
    for k, v in values.items():
        text = text.replace(k, v)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)
    for d in ("knowledge", "flows", "tickets"):
        (home / d).mkdir(parents=True, exist_ok=True)
    example = home / "knowledge" / "README.md"
    if not example.exists():
        example.write_text(resources.files("mobile_factory.templates").joinpath("knowledge-example.md").read_text())
    return [f"{k.strip('_').lower()}: {values[k] or '(not detected, fill in)'}" for k in detected]

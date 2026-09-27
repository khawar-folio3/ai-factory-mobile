from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..proc import which
from .base import CheckRun, Platform

HOME_BIN = Path.home() / ".maestro" / "bin" / "maestro"
LIBRARY = "maestro"  # saved flows: <home>/<flows_dir>/maestro/<name>.yaml
_SNAP = re.compile(r"^# snap (before|after): (.+)$")
_SETUP = {"defineVariablesCommand", "applyConfigurationCommand"}  # Maestro's own bookkeeping, not flow steps


def binary() -> str | None:
    return which("maestro") or (str(HOME_BIN) if HOME_BIN.is_file() else None)


def _rx(s: str) -> str:
    """Maestro text selectors are regexes: escape only what regex treats as special, keep it readable."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", s)


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _sel(hit: Mapping[str, str], fallback: str) -> str:
    """Text, then resource id, then content-desc; a point only when the node has nothing to select it by."""
    if hit.get("text"):
        return f"text: {_q(_rx(hit['text']))}"
    if hit.get("id"):
        return f"id: {_q(hit['id'])}"
    if hit.get("desc"):
        return f"text: {_q(_rx(hit['desc']))}"
    if hit.get("point"):
        return f"point: {_q(hit['point'])}"
    return f"text: {_q('.*' + _rx(fallback) + '.*')}"


def tap(hit: Mapping[str, str], label: str, nth: int = 1) -> list[str]:
    return [f"- tapOn: {{{_sel(hit, label)}{f', index: {nth - 1}' if nth > 1 else ''}}}"]


def wait(hit: Mapping[str, str], text: str, timeout: int) -> list[str]:
    return [f"- extendedWaitUntil: {{visible: {{{_sel(hit, text)}}}, timeout: {timeout * 1000}}}"]


def type_text(text: str) -> list[str]:
    return [f"- inputText: {_q(text)}"]


def open_link(link: str) -> list[str]:
    return [f"- openLink: {_q(link)}"]


def scroll(direction: str) -> list[str]:
    return ["- scroll"] if direction == "down" else ["- swipe: {direction: DOWN}"]


def snap(phase: str, label: str) -> list[str]:
    return [f"# snap {phase}: {label}", f"- takeScreenshot: {_q(f'{phase}-{label}')}"]


def record(flow: Path, app_id: str, lines: list[str]) -> None:
    """Append steps to a Maestro flow; a new flow starts from a fresh launch so it replays from anywhere.
    A flow the agent wrote (it has `# criterion N:` sections) is never appended to."""
    if not app_id or not lines or (flow.is_file() and has_criteria(flow.read_text())):
        return
    if not flow.is_file():
        flow.parent.mkdir(parents=True, exist_ok=True)
        start = [] if lines[0] == "- launchApp" else ["- launchApp"]
        flow.write_text("\n".join([f"appId: {app_id}", "---", *start]) + "\n")
    with flow.open("a") as f:
        f.write("\n".join(lines) + "\n")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def matching(library: Path, screens: list[str]) -> list[Path]:
    """Saved project flows whose name matches a screen the plan checks."""
    want = {w for s in screens if (w := _slug(s))}
    have = sorted(library.glob("*.yaml")) if library.is_dir() else []
    return [f for f in have if any(w in _slug(f.stem) or _slug(f.stem) in w for w in want)]


def chunks(text: str) -> list[tuple[str, str]]:
    """(flow text, snap label) per `# snap` marker; screenshots are dropped: the factory snapshot replaces them."""
    head, _, body = text.partition("\n---\n")
    out: list[tuple[list[str], str]] = []
    cur: list[str] = []
    for ln in body.splitlines():
        if m := _SNAP.match(ln):
            out.append((cur, m[2]))
            cur = []
        elif ln.strip() and not ln.startswith("- takeScreenshot"):
            cur.append(ln)
    out.append((cur, ""))
    return [(f"{head}\n---\n" + "\n".join(c) + "\n" if c else "", lb) for c, lb in out if c or lb]


def _brief(cmd: dict[str, Any]) -> str:
    name, val = next(iter(cmd.items()), ("?", {}))
    if isinstance(val, dict):
        sel = val.get("selector") or val.get("condition", {}).get("visible") or val
        for k in ("textRegex", "idRegex", "text", "link", "point"):
            if isinstance(sel, dict) and isinstance(sel.get(k), str):
                return f"{name.removesuffix('Command')} {sel[k][:60]}"
    return name.removesuffix("Command")


def steps(debug_dir: Path) -> list[dict[str, Any]]:
    """Per-command status and duration from `maestro test --debug-output` (commands-*.json; 2.x: commands.json).
    `seq`: the top-level command's index in the flow (nested ones share it); an optional command that failed
    (WARNED) is not ok; `png`: Maestro's screenshot of a failure; `shot`: a `takeScreenshot` image."""
    out = []
    for f in sorted(debug_dir.rglob("commands*.json")) if debug_dir.is_dir() else []:
        try:
            entries = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        seq = -1
        for e in entries if isinstance(entries, list) else []:
            meta = e.get("metadata", {})
            if next(iter(e.get("command", {})), "") in _SETUP:
                continue
            seq += not meta.get("depth")
            if meta.get("status") not in ("COMPLETED", "FAILED", "WARNED"):
                continue
            err = meta.get("error") or ({"message": "assertion is false"} if meta["status"] == "WARNED" else {})
            arts = {a.get("type"): f.parent / a["path"] for a in meta.get("artifacts") or [] if a.get("path")}
            out.append(
                {
                    "step": _brief(e.get("command", {})),
                    "ok": meta["status"] == "COMPLETED",
                    "duration_ms": int(meta.get("duration") or 0),
                    "seq": seq,
                    **({"error": str(err.get("message", err))[:200]} if err else {}),
                    **({"png": str(arts["SCREENSHOT"])} if "SCREENSHOT" in arts else {}),
                    **({"shot": str(arts["TAKE_SCREENSHOT"])} if "TAKE_SCREENSHOT" in arts else {}),
                }
            )
    return out


def replay(p: Platform, flow: Path, work: Path, snaps: Path | None = None, phase: str = "after") -> CheckRun:
    """Run a flow; with `snaps`, run it between its `# snap` markers and take a factory snapshot at each one."""
    if snaps is None:
        return p.run_flow(flow, work)
    done: list[dict[str, Any]] = []
    for i, (body, label) in enumerate(chunks(flow.read_text()), 1):
        if body:
            part = work / f"{flow.stem}.part{i}.yaml"
            part.write_text(body)
            res = p.run_flow(part, work)
            done += res.steps
            if not res.ok:
                return CheckRun(False, res.summary, res.log, done)
        if label:
            t0 = time.monotonic()
            p.snapshot(snaps, phase, label)
            done.append(
                {"step": f"snap {phase} {label}", "ok": True, "duration_ms": int((time.monotonic() - t0) * 1000)}
            )
    return CheckRun(True, f"ok: replayed {flow.name}", None, done)


_CRIT = re.compile(r"^#\s*criterion\b")


def has_criteria(text: str) -> bool:
    return any(_CRIT.match(ln) for ln in text.splitlines())

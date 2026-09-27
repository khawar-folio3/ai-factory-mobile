from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import events

DEVICE = "device.jsonl"  # <run>/logs/device.jsonl: one line per `factory android …` command
CATEGORIES = ("agent", "device", "gradle", "waiting on human", "runner")


def device_log(run_dir: Path) -> Path:
    return run_dir / "logs" / DEVICE


def log_device(
    run_dir: Path,
    step: str,
    command: str,
    args: dict[str, Any],
    duration_ms: int,
    *,
    ok: bool,
    result: str = "",
    **extra: Any,
) -> dict[str, Any]:
    rec = {
        "ts": events.now(),
        "step": step,
        "command": command,
        "args": args,
        "duration_ms": duration_ms,
        "ok": ok,
        "result": " ".join(result.split())[:200],
        **extra,
    }
    f = device_log(run_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    return rec


def read_device(run_dir: Path) -> list[dict[str, Any]]:
    f = device_log(run_dir)
    return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()] if f.is_file() else []


@dataclass
class StepTime:
    step: str
    kind: str  # agent | gradle | auto
    total_ms: int = 0
    human_ms: int = 0
    device_ms: int = 0
    gradle_ms: int = 0
    skipped: bool = False
    cmds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rest_ms(self) -> int:
        return max(0, self.total_ms - self.human_ms - self.device_ms - self.gradle_ms)


def _ts(e: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(e["ts"])


def steps(evs: list[dict[str, Any]], device: list[dict[str, Any]], kinds: dict[str, str]) -> list[StepTime]:
    """Per step: time from node events (every attempt), human waits at gates, device and gradle time from commands."""
    by: dict[str, StepTime] = {}

    def get(name: str) -> StepTime:
        return by.setdefault(name, StepTime(name, kinds.get(name, "auto")))

    waits: dict[tuple[str, str], datetime] = {}
    for e in evs:
        n = e.get("node") or ""
        if e["type"] in (events.NODE_COMPLETED, events.NODE_FAILED) and n:
            s = get(n)
            s.total_ms += int(e.get("duration_ms") or 0)
            s.skipped = s.skipped or bool(e.get("skipped"))
        elif e["type"] == events.GATE_WAITING and n:
            waits[(n, e.get("gate", ""))] = _ts(e)
        elif e["type"] == events.GATE_DECIDED and (t0 := waits.pop((n, e.get("gate", "")), None)):
            get(n).human_ms += int((_ts(e) - t0).total_seconds() * 1000)
    for r in device:
        s = get(r.get("step") or "?")
        s.cmds.append(r)
        if r["command"] == "install":
            s.gradle_ms += int(r.get("duration_ms") or 0)
        else:
            s.device_ms += int(r.get("duration_ms") or 0)
    for s in by.values():
        s.total_ms = max(s.total_ms, s.human_ms + s.device_ms + s.gradle_ms)  # commands outside a timed attempt
        if s.kind == "gradle":
            s.gradle_ms = s.total_ms - s.human_ms - s.device_ms
    return list(by.values())


def summary(sts: list[StepTime]) -> dict[str, int]:
    cat = dict.fromkeys(CATEGORIES, 0)
    for s in sts:
        cat["waiting on human"] += s.human_ms
        cat["device"] += s.device_ms
        cat["gradle"] += s.gradle_ms
        cat["agent" if s.kind == "agent" else "runner"] += s.rest_ms
    return cat


def dur(ms: int) -> str:
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    if ms < 3_600_000:
        return f"{ms // 60_000}m{ms // 1000 % 60:02d}s"
    return f"{ms // 3_600_000}h{ms // 60_000 % 60:02d}m"


def _cmd(r: dict[str, Any]) -> str:
    arg = next((str(v) for v in r.get("args", {}).values() if v not in ("", None)), "")
    return f"{r['command']} '{arg[:30]}'" if arg else r["command"]


def render_summary(sts: list[StepTime]) -> str:
    cat = summary(sts)
    total = sum(cat.values()) or 1
    rows = [f"{'category':<17}| {'time':>8} | share", f"{'-' * 17}|{'-' * 10}|------"]
    rows += [f"{c:<17}| {dur(ms):>8} | {ms * 100 // total:>3}%" for c, ms in cat.items() if ms or c != "runner"]
    return "\n".join([*rows, f"{'total':<17}| {dur(sum(cat.values())):>8} |"])


def render_steps(sts: list[StepTime]) -> str:
    cols = ("time", "cmds", "device", "gradle", "human", "rest")
    rows = [f"{'step':<14}" + "".join(f"{c:>8}" for c in cols) + "  slowest"]
    for s in sts:
        if s.skipped and not s.total_ms:
            rows.append(f"{s.step:<14}{'skipped':>8}")
            continue
        slow = sorted(s.cmds, key=lambda r: -int(r.get("duration_ms") or 0))[:3]
        nums = (dur(s.total_ms), len(s.cmds), dur(s.device_ms), dur(s.gradle_ms), dur(s.human_ms), dur(s.rest_ms))
        line = f"{s.step:<14}" + "".join(f"{n:>8}" for n in nums)
        worst = ", ".join(f"{_cmd(r)} {dur(int(r['duration_ms']))}" for r in slow)
        rows.append(f"{line}  {worst}".rstrip())
        rows += [f"{'':<14}  ✗ {_cmd(r)}: {r.get('result', '')[:90]}" for r in s.cmds if not r.get("ok")]
    return "\n".join(rows)


def render_step(s: StepTime) -> str:
    rows = [
        f"{s.step}: {dur(s.total_ms)} · device {dur(s.device_ms)} · gradle {dur(s.gradle_ms)} · human {dur(s.human_ms)}"
    ]
    for r in s.cmds:
        rows.append(
            f"  {r['ts'][11:19]} {'✓' if r.get('ok') else '✗'} {dur(int(r['duration_ms'])):>7}  {_cmd(r)}  {r.get('result', '')[:80]}"
        )
        rows += [
            f"      {'✓' if x.get('ok') else '✗'} {dur(int(x.get('duration_ms') or 0)):>7}  {x['step']}"
            for x in r.get("steps", [])
        ]
    return "\n".join(rows)

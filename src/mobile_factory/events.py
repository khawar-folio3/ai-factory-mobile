from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Event = dict[str, Any]
Sink = Callable[[Event], None]

# Event types (stable, documented in docs/events.md)
RUN_STARTED = "run.started"
RUN_FINISHED = "run.finished"
NODE_STARTED = "node.started"
NODE_COMPLETED = "node.completed"
NODE_FAILED = "node.failed"
AGENT_WAITING = "agent.waiting"
GATE_WAITING = "gate.waiting"
GATE_DECIDED = "gate.decided"
RISK_ASSESSED = "risk.assessed"
ROLLBACK = "checkpoint.rollback"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class EventBus:
    def __init__(self, log: Path, sinks: list[Sink] | None = None) -> None:
        self.log = log
        self.sinks = sinks or []

    def emit(self, type: str, run_id: str, **data: Any) -> Event:
        ev: Event = {"ts": now(), "type": type, "run": run_id, **data}
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a") as f:
            f.write(json.dumps(ev, default=str) + "\n")
        for sink in self.sinks:
            try:
                sink(ev)
            except Exception:  # noqa: S112 - a sink must never break a run
                continue
        return ev


def _post(url: str, body: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 2.0) -> None:
    req = urllib.request.Request(  # noqa: S310 - URLs come from the user's own config / local registry
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(headers or {})}
    )
    urllib.request.urlopen(req, timeout=timeout).close()  # noqa: S310


def slack_sink(webhook: str) -> Sink:
    def sink(ev: Event) -> None:
        if ev["type"] == GATE_WAITING:
            text = (
                f":raised_hand: *{ev.get('ticket')}* waits at gate *{ev['gate']}* "
                f"(risk {ev.get('risk')}, autonomy L{ev.get('level')}).\n{ev.get('summary', '')}\n"
                f"Approve in the repo: `factory approve {ev['gate']}`"
            )
        elif ev["type"] == RUN_FINISHED:
            text = f"*{ev.get('ticket')}* finished: `{ev.get('outcome')}` {ev.get('pr_url') or ''}".strip()
        else:
            return
        _post(webhook, {"text": text})

    return sink


class PixelAgentsSink:
    """Pixel Agents compat bridge: replays factory events as Claude-Code-shaped hook payloads to every live
    Pixel Agents server (~/.pixel-agents/servers/*.json). Nodes appear as tools, gates as permission prompts."""

    home = Path.home() / ".pixel-agents"

    def __init__(self, cwd: Path) -> None:
        self.cwd = str(cwd)

    def servers(self) -> list[dict[str, Any]]:
        files = sorted((self.home / "servers").glob("*.json")) or [self.home / "server.json"]
        live = []
        for f in files:
            try:
                s = json.loads(f.read_text())
                os.kill(int(s["pid"]), 0)
                live.append(s)
            except (OSError, ValueError, KeyError):
                continue
        return live

    def payloads(self, ev: Event) -> list[dict[str, Any]]:
        base = {"session_id": f"factory-{ev['run']}", "cwd": self.cwd}
        t = ev["type"]
        if t == RUN_STARTED:
            return [{**base, "hook_event_name": "SessionStart", "source": "mobile-factory"}]
        if t == NODE_STARTED:
            return [
                {
                    **base,
                    "hook_event_name": "PreToolUse",
                    "tool_name": f"factory:{ev['node']}",
                    "tool_input": {"description": ev.get("title", ev["node"])},
                }
            ]
        if t in (NODE_COMPLETED, NODE_FAILED, GATE_DECIDED):
            return [{**base, "hook_event_name": "PostToolUse"}]
        if t == GATE_WAITING:
            return [{**base, "hook_event_name": "Notification", "notification_type": "permission_prompt"}]
        if t == AGENT_WAITING:
            return [{**base, "hook_event_name": "Stop"}]
        if t == RUN_FINISHED:
            return [
                {**base, "hook_event_name": "Stop"},
                {**base, "hook_event_name": "SessionEnd", "reason": ev.get("outcome")},
            ]
        return []

    def __call__(self, ev: Event) -> None:
        for body in self.payloads(ev):
            for s in self.servers():
                try:
                    _post(
                        f"http://127.0.0.1:{s['port']}/api/hooks/claude",
                        body,
                        {"Authorization": f"Bearer {s['token']}"},
                        timeout=1.0,
                    )
                except OSError:
                    continue


def read(log: Path, run_id: str | None = None) -> list[Event]:
    if not log.is_file():
        return []
    evs = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    return [e for e in evs if run_id is None or e.get("run") == run_id]

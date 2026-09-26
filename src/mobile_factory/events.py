from __future__ import annotations

import json
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


def read(log: Path, run_id: str | None = None) -> list[Event]:
    if not log.is_file():
        return []
    evs = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    return [e for e in evs if run_id is None or e.get("run") == run_id]

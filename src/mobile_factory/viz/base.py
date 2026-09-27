from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import events

if TYPE_CHECKING:
    from ..config import VizConfig

# What a visualiser can show. Each backend maps these to its own protocol.
LABEL_MAX = 14  # characters: labels float over characters and overlap when longer
Kind = Literal["begin", "step", "step_done", "waiting", "idle", "subagent", "subagent_done", "end"]


@dataclass(frozen=True)
class Office:
    """A running visualiser the developer can open."""

    url: str
    pid: int | None = None


class Visualizer:
    """One tool that shows factory work and the agents doing it. Subclass it to add a tool; this base does nothing,
    so a missing or disabled tool never breaks a run."""

    name = "none"
    title = "no visualiser"
    supports_live_hooks = False  # can it see agent sessions (and their subagents) by itself?

    def installed(self) -> bool:
        return True

    def offices(self) -> list[Office]:
        return []

    def live_hooks(self) -> bool:
        return False

    def prepare(self, root: Path, prefs: VizConfig) -> list[str]:
        """Apply the developer's preferences (settings, layout, seating); returns one line per thing it changed."""
        return []

    def restart_needed(self, prefs: VizConfig) -> bool:
        return False

    def start(self, root: Path, log: Path) -> Office | None:
        return None

    def stop(self) -> None:
        return None

    def send(self, root: Path, session: str, kind: Kind, **fields: Any) -> None:
        return None


class NullVisualizer(Visualizer):
    pass


class Session:
    """One watchable piece of work (a factory run, a harvest, a distill): begin → steps and subagents → end.
    Every call is best-effort: a visualiser that is down or broken never interrupts the work."""

    def __init__(self, viz: Visualizer, root: Path, name: str) -> None:
        self.viz, self.root = viz, root
        self.id = f"{name}-{int(time.time() * 1000)}"

    def _send(self, kind: Kind, **fields: Any) -> None:
        with contextlib.suppress(Exception):
            self.viz.send(self.root, self.id, kind, **fields)

    def begin(self) -> None:
        self._send("begin")

    def step(self, title: str, short: str = "") -> None:
        """`short` is the on-screen label (≤ LABEL_MAX); defaults to the title's first word."""
        self.current = (title, label(short or title))
        self._last = time.monotonic()
        self._send("step", title=title, short=self.current[1])

    def pulse(self, every: float = 5.0) -> None:
        """Re-send the current step now and then: visualisers show a quiet session as idle."""
        if getattr(self, "current", None) and time.monotonic() - self._last >= every:
            self._send("step_done", title=self.current[0])
            self.step(*self.current)

    def step_done(self, title: str = "") -> None:
        self._send("step_done", title=title)

    def waiting(self) -> None:
        self._send("waiting")

    def idle(self) -> None:
        self._send("idle")

    def subagent(self, name: str) -> None:
        self._subs = getattr(self, "_subs", 0) + 1
        self._open = [*getattr(self, "_open", []), self._subs]
        self._send("subagent", title=label(name), n=self._subs)

    def subagent_done(self) -> None:
        """Closes the oldest subagent still open."""
        open_ = getattr(self, "_open", [])
        if open_:
            self._send("subagent_done", n=open_.pop(0))

    def end(self, outcome: str = "done") -> None:
        self._send("end", outcome=outcome)


def label(text: str) -> str:
    """A label that fits: the text itself when short, else its first word, cut to LABEL_MAX."""
    text = " ".join(text.split())
    if len(text) <= LABEL_MAX:
        return text
    return (text.split(" ")[0] or text)[:LABEL_MAX]


_RUN_EVENTS: dict[str, Kind] = {
    events.RUN_STARTED: "begin",
    events.NODE_STARTED: "step",
    events.NODE_COMPLETED: "step_done",
    events.NODE_FAILED: "step_done",
    events.GATE_DECIDED: "step_done",
    events.GATE_WAITING: "waiting",
    events.AGENT_WAITING: "idle",
    events.RUN_FINISHED: "end",
}


def event_sink(viz: Visualizer, root: Path) -> events.Sink:
    """Factory run events → the visualiser: the run is one session, each node a step, each gate a wait."""

    def sink(ev: events.Event) -> None:
        kind = _RUN_EVENTS.get(ev["type"])
        if kind:
            title = str(ev.get("title") or ev.get("node") or "")
            with contextlib.suppress(Exception):
                short = label(str(ev.get("node") or title))  # node names are already short: work, checks …
                viz.send(root, f"run-{ev['run']}", kind, title=title, short=short, outcome=str(ev.get("outcome") or ""))

    return sink

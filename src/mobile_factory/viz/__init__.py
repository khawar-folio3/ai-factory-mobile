"""Visualisers: somewhere a developer can watch factory work and the agents doing it.

The factory only talks to `Visualizer` and `Session`. To add or swap a tool, subclass `Visualizer` in a new module,
register it in `BACKENDS`, and set `viz.tool` in the config. Try any backend without the factory: `factory viz demo`
(or `python -m mobile_factory.viz demo`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Kind, NullVisualizer, Office, Session, Visualizer, event_sink
from .claude_office import ClaudeOffice

if TYPE_CHECKING:
    from ..config import VizConfig

BACKENDS: dict[str, type[Visualizer]] = {ClaudeOffice.name: ClaudeOffice}


def make(prefs: VizConfig | None) -> Visualizer:
    """The developer's visualiser, or a no-op one when it is off or unknown."""
    if not prefs or not prefs.pixel_agents:
        return NullVisualizer()
    return BACKENDS.get(prefs.tool, NullVisualizer)()


__all__ = ["BACKENDS", "Kind", "NullVisualizer", "Office", "Session", "Visualizer", "event_sink", "make"]

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..events import _post
from ..proc import has, which
from . import pixel_layout
from .base import LABEL_MAX, Kind, Office, Visualizer

if TYPE_CHECKING:
    from ..config import VizConfig


class PixelAgents(Visualizer):
    """Pixel Agents (github.com/pixel-agents-hq/pixel-agents): agents as pixel-art characters in an office.
    Factory sessions are sent as Claude-Code-shaped hook events to every live server in ~/.pixel-agents/servers;
    with its own hooks on, the office also shows Claude Code sessions and their subagents by itself."""

    name = "pixel-agents"
    title = "Pixel Agents office"
    supports_live_hooks = True
    home = Path.home() / ".pixel-agents"

    # ---- the office ----

    def installed(self) -> bool:
        return has("pixel-agents")

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

    @staticmethod
    def url(server: dict[str, Any]) -> str:
        """The tokened URL, as `pixel-agents` prints it: only a page opened with the token may turn hooks on."""
        base = f"http://127.0.0.1:{server.get('port')}/"
        return f"{base}?token={server['token']}" if server.get("token") else base

    def offices(self) -> list[Office]:
        return [Office(self.url(s), int(s["pid"]) if s.get("pid") else None) for s in self.servers()]

    def live_hooks(self, claude_home: Path | None = None) -> bool:
        """Instant Detection: Pixel Agents' hook in ~/.claude/settings.json. Without it the office only guesses from
        transcripts, so a session waiting on subagents looks idle and subagents may not appear."""
        f = (claude_home or Path.home() / ".claude") / "settings.json"
        return f.is_file() and "pixel-agents" in json.dumps(json.loads(f.read_text()).get("hooks", {}))

    def prepare(self, root: Path, prefs: VizConfig) -> list[str]:
        self.prefer(labels=prefs.pixel_labels, hooks=prefs.pixel_hooks)
        notes = []
        pixel_layout.drop_old_mappings(self.home)
        if pixel_layout.ensure_layout(self.home) == "written":
            desks = pixel_layout.PODS_ACROSS * pixel_layout.PODS_DOWN
            notes.append(
                f"open office layout ({desks} spaced-out desks, one seat each; your own layout is never replaced)"
            )
        return notes

    def prefer(self, labels: bool, hooks: bool) -> None:
        """Office settings in ~/.pixel-agents/config.json; with consent granted, pixel-agents installs its own hook
        into ~/.claude/settings.json the next time it starts."""
        f = self.home / "config.json"
        cfg = json.loads(f.read_text()) if f.is_file() else {}
        cfg.setdefault("standalone", {})["alwaysShowLabels"] = labels
        if hooks:
            cfg.setdefault("hooksConsent", {})["claude"] = "granted"
            cfg.setdefault("hooksEnabled", {})["claude"] = True
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.factory-tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        tmp.replace(f)

    def restart_needed(self, prefs: VizConfig) -> bool:
        return bool(prefs.pixel_hooks and self.servers() and not self.live_hooks())

    def start(self, root: Path, log: Path) -> Office | None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as fh:
            proc = subprocess.Popen(  # detached: the office outlives the factory command that started it
                [which("pixel-agents") or "pixel-agents"],
                cwd=root,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        for _ in range(100):
            if (live := self.servers()) or proc.poll() is not None:
                break
            time.sleep(0.2)
        live = self.servers()
        return Office(self.url(live[0]), proc.pid) if live else None

    def stop(self) -> None:
        for s in self.servers():
            with contextlib.suppress(OSError, ValueError, KeyError):
                os.kill(int(s["pid"]), signal.SIGTERM)
        for _ in range(50):  # it removes its own registration on the way out
            if not self.servers():
                return
            time.sleep(0.1)

    # ---- sessions ----

    @staticmethod
    def payloads(root: Path, session: str, kind: Kind, **fields: Any) -> list[dict[str, Any]]:
        base = {"session_id": f"factory-{session}", "cwd": str(root)}
        title = str(fields.get("title") or "")
        short = str(fields.get("short") or title)[:LABEL_MAX] or "step"  # shown as "Using <short>"
        match kind:
            case "begin":  # the office creates the character on the event after SessionStart; a no-op tool end
                # does that, so the session's first real step is not swallowed by the creation
                return [
                    {**base, "hook_event_name": "SessionStart", "source": "mobile-factory"},
                    {**base, "hook_event_name": "PostToolUse"},
                ]
            case "step":
                return [
                    {
                        **base,
                        "hook_event_name": "PreToolUse",
                        "tool_name": short,
                        "tool_input": {"description": title},
                    }
                ]
            case "subagent":  # the office spawns a subagent character for an Agent tool call
                return [
                    {
                        **base,
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Agent",
                        "tool_input": {"description": title[:LABEL_MAX]},  # shown as "Subtask: <title>"
                    }
                ]
            case "step_done" | "subagent_done":
                return [{**base, "hook_event_name": "PostToolUse"}]
            case "waiting":
                return [{**base, "hook_event_name": "Notification", "notification_type": "permission_prompt"}]
            case "idle":
                return [{**base, "hook_event_name": "Stop"}]
            case "end":
                return [
                    {**base, "hook_event_name": "Stop"},
                    {**base, "hook_event_name": "SessionEnd", "reason": fields.get("outcome") or "done"},
                ]
        return []

    def send(self, root: Path, session: str, kind: Kind, **fields: Any) -> None:
        servers = self.servers()
        for body in self.payloads(root, session, kind, **fields):
            for s in servers:
                with contextlib.suppress(OSError):
                    _post(
                        f"http://127.0.0.1:{s['port']}/api/hooks/claude",
                        body,
                        {"Authorization": f"Bearer {s['token']}"},
                        timeout=1.0,
                    )

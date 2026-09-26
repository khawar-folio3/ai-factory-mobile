from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import state_home
from ..events import _post
from ..proc import has, which
from .base import LABEL_MAX, Kind, Office, Visualizer

if TYPE_CHECKING:
    from ..config import VizConfig

REPO = "https://github.com/paulrobello/claude-office"
PORT = 8000


class ClaudeOffice(Visualizer):
    """Claude Office (github.com/paulrobello/claude-office): a boss agent and its subagent employees, each at their
    own desk. One local process (FastAPI serving the built UI). Factory sessions are pushed to its events API; with
    its hooks installed it also shows Claude Code sessions and their subagents by itself."""

    name = "claude-office"
    title = "Claude Office"
    supports_live_hooks = True

    @property
    def home(self) -> Path:
        return state_home() / "tools" / "claude-office"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{PORT}"

    def installed(self) -> bool:
        return (self.home / "backend" / "static").is_dir() and has("uv")

    def _up(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/health", timeout=1) as r:  # noqa: S310 - fixed local URL
                return bool(r.status == 200)
        except OSError:
            return False

    def _pid_file(self) -> Path:
        return state_home() / "logs" / "claude-office.pid"

    def offices(self) -> list[Office]:
        if not self._up():
            return []
        pid = self._pid_file().read_text().strip() if self._pid_file().is_file() else ""
        return [Office(f"{self.base}/", int(pid) if pid.isdigit() else None)]

    def live_hooks(self, claude_home: Path | None = None) -> bool:
        f = (claude_home or Path.home() / ".claude") / "settings.json"
        return f.is_file() and "claude-office-hook" in json.dumps(json.loads(f.read_text()).get("hooks", {}))

    def prepare(self, root: Path, prefs: VizConfig) -> list[str]:
        if prefs.pixel_hooks and self.installed() and not self.live_hooks():
            code = subprocess.call(  # its own installer: adds `claude-office-hook` to ~/.claude/settings.json
                ["./install.sh"], cwd=self.home / "hooks", stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
            )
            if code == 0:
                return ["live hooks installed (Claude Code sessions and subagents show by themselves)"]
        return []

    def start(self, root: Path, log: Path) -> Office | None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as fh:
            proc = subprocess.Popen(  # detached: the office outlives the factory command that started it
                [which("uv") or "uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
                cwd=self.home / "backend",
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "SERVE_STATIC": "1"},  # one process: the API also serves the built UI
            )
        self._pid_file().write_text(f"{proc.pid}\n")
        for _ in range(150):
            if self._up() or proc.poll() is not None:
                break
            time.sleep(0.2)
        return Office(f"{self.base}/", proc.pid) if self._up() else None

    def stop(self) -> None:
        if self._pid_file().is_file():
            with contextlib.suppress(OSError, ValueError):
                os.killpg(int(self._pid_file().read_text().strip()), signal.SIGTERM)
            self._pid_file().unlink(missing_ok=True)

    # ---- sessions ----

    @staticmethod
    def payloads(root: Path, session: str, kind: Kind, **fields: Any) -> list[dict[str, Any]]:
        title = str(fields.get("title") or "")
        short = str(fields.get("short") or title)[:LABEL_MAX] or "step"
        where = {"project_name": root.name, "project_dir": str(root), "working_dir": str(root)}

        def ev(event_type: str, **data: Any) -> dict[str, Any]:
            return {
                "event_type": event_type,
                "session_id": f"factory-{session}"[:128],
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {**where, **data},
            }

        n = fields.get("n", 0)
        match kind:
            case "begin":
                return [ev("session_start")]
            case "step":
                return [ev("pre_tool_use", tool_name=short, tool_use_id=f"{session}-{short}")]
            case "step_done":
                return [ev("post_tool_use", tool_name=short, tool_use_id=f"{session}-{short}", success=True)]
            case "subagent":
                aid = f"{session}-sub-{n}"
                return [ev("subagent_start", agent_id=aid, agent_name=title[:LABEL_MAX], task_description=title)]
            case "subagent_done":
                return [ev("subagent_stop", agent_id=f"{session}-sub-{n}", success=True)]
            case "waiting":
                return [ev("permission_request", tool_name="gate")]
            case "idle":
                return [ev("stop")]
            case "end":
                return [ev("stop"), ev("session_end", reason=str(fields.get("outcome") or "done"))]
        return []

    def send(self, root: Path, session: str, kind: Kind, **fields: Any) -> None:
        for body in self.payloads(root, session, kind, **fields):
            with contextlib.suppress(OSError):
                _post(f"{self.base}/api/v1/events", body, timeout=1.0)

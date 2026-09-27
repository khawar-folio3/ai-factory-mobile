from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import RunState

FIELDS = ("input", "cache_write", "cache_read", "output")


@dataclass
class Usage:
    """Tokens a ticket's run cost, per model, from the Claude Code transcripts of the sessions that drove it."""

    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)

    def add(self, model: str, u: dict[str, Any]) -> None:
        row = self.by_model.setdefault(_tier(model), dict.fromkeys(FIELDS, 0))
        row["input"] += int(u.get("input_tokens") or 0)
        row["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        row["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        row["output"] += int(u.get("output_tokens") or 0)

    @property
    def total(self) -> int:
        return sum(sum(r.values()) for r in self.by_model.values())

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "by_model": self.by_model, "sessions": self.sessions}


def _tier(model: str) -> str:
    m = re.search(r"(opus|sonnet|haiku|fable)", model or "")
    return m.group(1) if m else (model or "unknown")


def projects_dir(root: Path) -> Path:
    """Where Claude Code keeps this repo's transcripts: the path with every non-alphanumeric turned into '-'."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root.resolve()))


def _when(ts: str) -> datetime:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _records(f: Path) -> Iterable[dict[str, Any]]:
    try:
        with f.open(errors="ignore") as fh:
            for ln in fh:
                if '"usage"' in ln:
                    try:
                        yield json.loads(ln)
                    except ValueError:
                        continue
    except OSError:
        return


def for_run(st: RunState, root: Path, until: datetime | None = None) -> Usage:
    """Sessions whose transcript mentions this run's id (every `factory next` prints it) are its drivers; their
    usage inside the run's time window counts, with the subagents they started (…/<session>/subagents/*.jsonl)."""
    start = _when(st.created_at)
    end = until or (_when(st.updated_at) if st.finished else datetime.now(UTC))
    out, seen = Usage(), set()
    base = projects_dir(root)
    for f in sorted(base.glob("*.jsonl")) if base.is_dir() else []:
        if datetime.fromtimestamp(f.stat().st_mtime, UTC) < start or st.id not in f.read_text(errors="ignore"):
            continue
        out.sessions.append(f.stem)
        for t in [f, *sorted((base / f.stem / "subagents").glob("*.jsonl"))]:
            for rec in _records(t):
                msg = rec.get("message") or {}
                key = msg.get("id") or rec.get("requestId")
                ts = rec.get("timestamp")
                if rec.get("type") != "assistant" or not msg.get("usage") or not ts or key in seen:
                    continue
                if start <= _when(ts) <= end:
                    seen.add(key)  # one API response is logged once per content block
                    out.add(str(msg.get("model", "")), msg["usage"])
    return out


def render(u: Usage) -> str:
    if not u.sessions:
        return "tokens: no Claude Code session found for this run (Cursor runs are not counted yet)"
    lines = [f"tokens: {u.total:,} total across {len(u.sessions)} session(s)"]
    for model, r in sorted(u.by_model.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(
            f"  {model:<7} {sum(r.values()):>12,}  (output {r['output']:,} · input {r['input']:,}"
            f" · cache write {r['cache_write']:,} · cache read {r['cache_read']:,})"
        )
    return "\n".join(lines)

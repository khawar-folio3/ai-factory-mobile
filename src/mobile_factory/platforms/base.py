from __future__ import annotations

import difflib
import hashlib
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import FactoryError


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    optional: bool = False


@dataclass(frozen=True)
class CheckRun:
    ok: bool
    summary: str
    log: Path | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)  # per-step results of a replayed flow


class Platform(ABC):
    name: str
    last_hit: dict[str, str]  # the node the last tap / wait matched (text, id, desc, point): the recorder's selector
    settle_ms: int = 0  # the last tap / open: input sent -> screen changed (or the settle cap)
    changed: bool = True  # the last tap / open changed the screen

    @abstractmethod
    def doctor(self) -> list[Check]: ...

    @abstractmethod
    def ensure_device(self) -> str: ...

    @abstractmethod
    def build_install(self, log_dir: Path, launch: bool = True) -> CheckRun: ...

    def modules_for(self, files: list[str]) -> list[str]:
        return []

    def build_context(self) -> dict[str, str]:
        """Module, variant, install/assemble/test tasks, package and deeplink scheme: resolved once, never guessed."""
        return {}

    @abstractmethod
    def checks(self, files: list[str], log_dir: Path) -> CheckRun:
        """Lint + unit tests for the modules the change touched."""

    @abstractmethod
    def screen_state(self, png: Path | None = None) -> str:
        """Stable, diffable text of what is on screen (no clock/battery noise); `png` adds what OCR reads on it."""

    @abstractmethod
    def screenshot(self, dest: Path) -> None: ...

    @abstractmethod
    def tap(self, label: str, nth: int = 1) -> str: ...

    @abstractmethod
    def wait_for(self, text: str, timeout: int = 30) -> bool: ...

    @abstractmethod
    def open_link(self, link: str) -> str: ...

    @abstractmethod
    def back(self) -> None: ...

    def type_text(self, text: str) -> str:
        raise FactoryError(f"{self.name}: typing is not supported")

    def scroll(self, direction: str = "down") -> str:
        raise FactoryError(f"{self.name}: scrolling is not supported")

    @abstractmethod
    def launch(self) -> str: ...

    @abstractmethod
    def run_flow(self, flow: Path, log_dir: Path) -> CheckRun: ...

    def mock_on(self, run_dir: Path) -> str:
        raise FactoryError(f"{self.name}: network mocks are not supported")

    def mock_off(self, run_dir: Path) -> str:
        return "no mocks"

    def snapshot(self, root: Path, phase: str, label: str) -> tuple[Path, str]:
        if phase not in ("before", "after"):
            raise ValueError("phase must be before|after")
        d = root / phase
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "_-" else "-" for c in label) or "screen"
        png = d / f"{safe}.png"
        self.screenshot(png)
        state = self.screen_state(png)
        png.with_suffix(".txt").write_text(state)
        return png, state


def files_hash(root: Path, files: Iterable[str], salt: str = "") -> str:
    """Names + contents of `files` (a missing file hashes as deleted): equal hashes mean the same sources."""
    h = hashlib.sha256(salt.encode())
    for f in sorted(set(files)):
        p = root / f
        h.update(f.encode() + b"\0" + (p.read_bytes() if p.is_file() else b"-") + b"\0")
    return h.hexdigest()[:16]


def worktree_hash(root: Path) -> str:
    """HEAD + every tracked and untracked change: what a build would compile."""

    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=False).stdout

    paths = [ln[3:].split(" -> ")[-1].strip('"') for ln in git("status", "--porcelain", "-uall").splitlines()]
    return files_hash(root, paths, git("rev-parse", "HEAD").strip())


def snapshot_diff(root: Path) -> list[tuple[str, str]]:
    """(label, 'unchanged' | 'only before' | changed lines) for every label captured in either phase."""
    before = {p.stem: p for p in (root / "before").glob("*.txt")}
    after = {p.stem: p for p in (root / "after").glob("*.txt")}
    out = []
    for label in sorted(before.keys() | after.keys()):
        if label not in after:
            out.append((label, "only before (not re-captured)"))
        elif label not in before:
            out.append((label, "only after"))
        else:
            a, b = before[label].read_text().splitlines(), after[label].read_text().splitlines()
            if a == b:
                out.append((label, "unchanged"))
            else:
                lines = [
                    ln
                    for ln in difflib.unified_diff(a, b, lineterm="", n=0)
                    if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
                ]
                out.append((label, "\n".join(f"    {ln[:300]}" for ln in lines)))
    return out

from __future__ import annotations

import difflib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


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


class Platform(ABC):
    name: str

    @abstractmethod
    def doctor(self) -> list[Check]: ...

    @abstractmethod
    def ensure_device(self) -> str: ...

    @abstractmethod
    def build_install(self, log_dir: Path, launch: bool = True) -> CheckRun: ...

    def modules_for(self, files: list[str]) -> list[str]:
        return []

    @abstractmethod
    def checks(self, files: list[str], log_dir: Path) -> CheckRun:
        """Lint + unit tests for the modules the change touched."""

    @abstractmethod
    def screen_state(self) -> str:
        """Stable, diffable text of what is on screen (no clock/battery noise)."""

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

    @abstractmethod
    def launch(self) -> str: ...

    @abstractmethod
    def run_flow(self, flow: Path, log_dir: Path) -> CheckRun: ...

    def snapshot(self, root: Path, phase: str, label: str) -> tuple[Path, str]:
        if phase not in ("before", "after"):
            raise ValueError("phase must be before|after")
        d = root / phase
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "_-" else "-" for c in label) or "screen"
        png = d / f"{safe}.png"
        self.screenshot(png)
        state = self.screen_state()
        png.with_suffix(".txt").write_text(state)
        return png, state


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

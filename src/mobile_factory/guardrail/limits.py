from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import ProjectConfig
from ..globs import matches
from .diff import FileDiff

SUPPRESSION = re.compile(
    r"@Suppress\b|@SuppressLint|@Ignore\b|@Disabled\b|tools:ignore|lint-baseline|swiftlint:disable|// ?noqa|#\s*noqa"
    r"|XCTSkip|@available\(\*, unavailable\)"
)


@dataclass
class LimitReport:
    forbidden: list[str] = field(default_factory=list)
    suppressions: list[str] = field(default_factory=list)
    deleted_tests: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.forbidden or self.suppressions or self.deleted_tests)

    def lines(self) -> list[str]:
        out = [f"forbidden path touched: {p}" for p in self.forbidden]
        out += [f"suppression / disabled test added: {s}" for s in self.suppressions]
        out += [f"test deleted: {p}" for p in self.deleted_tests]
        return out


def check(cfg: ProjectConfig, files: list[str], diffs: list[FileDiff], deleted: list[str]) -> LimitReport:
    rep = LimitReport()
    rep.forbidden = [f for f in files if matches(f, cfg.forbidden_paths)]
    for d in diffs:
        for n, text in d.added:
            if SUPPRESSION.search(text):
                rep.suppressions.append(f"{d.path}:{n}: {text.strip()[:120]}")
    rep.deleted_tests = [f for f in deleted if any(t in f"/{f}" for t in cfg.test_dirs)]
    return rep

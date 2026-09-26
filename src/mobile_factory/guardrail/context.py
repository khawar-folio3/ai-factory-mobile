from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import LoadedConfig
from . import diff as difflib
from . import rules as rl


@dataclass
class ReviewContext:
    dir: Path
    files: list[str]
    diff_lines: int
    skipped_secret: list[str]
    rules_loaded: int
    rules_total: int
    detector_findings: list[rl.Finding]

    def summary(self) -> str:
        return (
            f"changed files {len(self.files)} · diff lines {self.diff_lines} · secret-like skipped {len(self.skipped_secret)}"
            f" · rules loaded {self.rules_loaded}/{self.rules_total} · detector findings {len(self.detector_findings)}\n"
            f"context: {self.dir}/{{diff.patch,rules.md,detector.json}}"
        )


def build(lc: LoadedConfig, patch: str, out_dir: Path) -> ReviewContext:
    g = lc.cfg.guardrail
    out_dir.mkdir(parents=True, exist_ok=True)
    clean, skipped = difflib.strip_secrets(patch)
    diffs = difflib.parse(clean)
    (out_dir / "diff.patch").write_text(clean)

    knowledge = sorted(lc.path(g.knowledge).glob("*.md")) if lc.path(g.knowledge).is_dir() else []
    all_rules, tails = rl.load_rule_files([lc.path(g.taste), *knowledge])
    loaded, no_hit = rl.select(all_rules, diffs)

    parts = [f"{r.body.strip()}\n(source: {Path(r.source).name})" for r in loaded]
    if no_hit:
        parts.append(
            "## In scope, no keyword hit (not loaded; open the source file only if the diff clearly calls for it)\n"
            + "\n".join(f"- {r.id} · {r.title}" for r in no_hit)
        )
    parts += tails
    if not all_rules:
        parts.append(f"(no taste rules yet: run `factory guardrail learn` to build {g.taste})")
    (out_dir / "rules.md").write_text("\n\n".join(parts) + "\n")

    found = rl.detect(diffs, rl.slop_rules(lc.path(g.slop_overrides))) if g.slop else []
    (out_dir / "detector.json").write_text(json.dumps([f.model_dump() for f in found], indent=2))
    return ReviewContext(
        dir=out_dir,
        files=[d.path for d in diffs],
        diff_lines=clean.count("\n"),
        skipped_secret=skipped,
        rules_loaded=len(loaded),
        rules_total=len(all_rules),
        detector_findings=found,
    )

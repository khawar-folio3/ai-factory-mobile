from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ..globs import matches
from .diff import FileDiff

Severity = Literal["blocker", "major", "nit", "general"]
_RULE_HEAD = re.compile(r"^### ([A-Z]+\d+) · (.+?)\s*\[(blocker|major|nit)\]")


class Rule(BaseModel):
    id: str
    title: str
    severity: Severity
    applies: list[str] = Field(default_factory=lambda: ["**"])
    keywords: str = ""
    keywords_removed: str = ""
    body: str = ""
    source: str = ""


class SlopRule(BaseModel):
    id: str
    title: str
    severity: Severity
    applies: list[str]
    pattern: str
    fix: str = ""
    enabled: bool = True


class Finding(BaseModel):
    file: str
    line: int | None = None
    rule: str
    severity: Severity
    suggestion: str
    outcome: Literal["open", "applied", "not applied", "dismissed"] = "open"
    reason: str = ""
    source: Literal["detector", "agent"] = "agent"

    @model_validator(mode="after")
    def _reason_when_not_applied(self) -> Finding:
        if self.outcome in ("not applied", "dismissed") and not self.reason.strip():
            raise ValueError(f"{self.rule} at {self.file}: '{self.outcome}' needs a reason")
        return self

    @property
    def blocking(self) -> bool:
        return self.severity in ("blocker", "major") and self.outcome in ("open", "not applied")

    def same_spot(self, other: Finding) -> bool:
        return (self.rule, self.file, self.line) == (other.rule, other.file, other.line)


def parse_rules(text: str, source: str) -> tuple[list[Rule], str]:
    """Rule blocks plus the always-loaded tail (every `## ` section after the first rule block ends)."""
    rules: list[Rule] = []
    tail: list[str] = []
    cur: Rule | None = None
    in_tail = False
    for ln in text.splitlines():
        if m := _RULE_HEAD.match(ln):
            in_tail = False
            cur = Rule(id=m.group(1), title=m.group(2).strip(), severity=m.group(3), body=ln, source=source)
            rules.append(cur)
        elif ln.startswith("## ") and rules:
            in_tail, cur = True, None
            tail.append(ln)
        elif in_tail:
            tail.append(ln)
        elif cur is not None:
            key, _, val = ln.partition(":")
            if key == "applies":
                cur.applies = [g.strip() for g in val.split(",") if g.strip()]
            elif key == "keywords":
                cur.keywords = val.strip()
                continue
            elif key == "keywords-removed":
                cur.keywords_removed = val.strip()
                continue
            cur.body += "\n" + ln
    return rules, "\n".join(tail).strip()


def _hit(pattern: str, lines: list[str]) -> bool:
    try:
        rx = re.compile(pattern)
    except re.error:
        return True  # a broken keyword must not hide a rule
    return any(rx.search(ln) for ln in lines)


def select(rules: list[Rule], diffs: list[FileDiff]) -> tuple[list[Rule], list[Rule]]:
    """(loaded, in-scope-but-no-keyword-hit)."""
    paths = [d.path for d in diffs]
    added = paths + [t for d in diffs for _, t in d.added]
    removed = [t for d in diffs for t in d.removed]
    loaded, skipped = [], []
    for r in rules:
        if not any(matches(p, r.applies) for p in paths):
            continue
        if (
            not (r.keywords or r.keywords_removed)
            or (r.keywords and _hit(r.keywords, added))
            or (r.keywords_removed and _hit(r.keywords_removed, removed))
        ):
            loaded.append(r)
        else:
            skipped.append(r)
    return loaded, skipped


def load_rule_files(files: list[Path]) -> tuple[list[Rule], list[str]]:
    rules, tails = [], []
    for f in files:
        if f.is_file():
            r, t = parse_rules(f.read_text(), str(f))
            rules += r
            if t:
                tails.append(t)
    return rules, tails


def slop_rules(overrides: Path | None = None) -> list[SlopRule]:
    base = yaml.safe_load(resources.files("mobile_factory.templates").joinpath("slop.yaml").read_text())
    by_id = {r["id"]: r for r in base}
    if overrides and overrides.is_file():
        for r in yaml.safe_load(overrides.read_text()) or []:
            by_id[r["id"]] = {**by_id.get(r["id"], {}), **r}
    return [SlopRule.model_validate(r) for r in by_id.values() if r.get("enabled", True)]


def detect(diffs: list[FileDiff], rules: list[SlopRule]) -> list[Finding]:
    out: list[Finding] = []
    compiled = [(r, re.compile(r.pattern)) for r in rules]
    for d in diffs:
        for r, rx in compiled:
            if not matches(d.path, r.applies):
                continue
            for n, text in d.added:
                if rx.search(text):
                    out.append(
                        Finding(
                            file=d.path,
                            line=n,
                            rule=r.id,
                            severity=r.severity,
                            suggestion=f"{r.title}: {r.fix}".strip(": "),
                            source="detector",
                        )
                    )
    return out


def verdict(findings: list[Finding]) -> str:
    return "NEEDS WORK" if any(f.blocking for f in findings) else "READY FOR HUMAN REVIEW"


def table(findings: list[Finding]) -> str:
    counts = {
        s: sum(1 for f in findings if f.severity == s and f.outcome in ("open", "not applied"))
        for s in ("blocker", "major", "nit")
    }
    head = f"{verdict(findings)}  ({counts['blocker']} blocker, {counts['major']} major, {counts['nit']} nit open)"
    rows = ["| # | file:line | rule | severity | suggestion | outcome |", "|---|---|---|---|---|---|"]
    for i, f in enumerate(findings, 1):
        outcome = f.outcome + (f": {f.reason}" if f.reason else "")
        rows.append(f"| {i} | {f.file}:{f.line or '-'} | {f.rule} | {f.severity} | {f.suggestion} | {outcome} |")
    return "\n".join([head, *rows]) if findings else head

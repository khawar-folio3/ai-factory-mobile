from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SECRET_PATH = re.compile(
    r"(^|/)\.env($|[./])|\.(jks|keystore|pem|key|p12|pfx)$|(^|/)(local|keystore)\.properties$"
    r"|(secret|credential)[^/]*\.(json|ya?ml|properties)$|(^|/)google[-_]services[^/]*\.json$"
    r"|GoogleService-Info\.plist$"
)


@dataclass
class FileDiff:
    path: str
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def is_secret_path(path: str) -> bool:
    return bool(_SECRET_PATH.search(path))


def parse(patch: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    line_no = 0
    for ln in patch.splitlines():
        if ln.startswith("diff --git "):
            path = ln.split(" b/", 1)[-1]
            cur = FileDiff(path)
            files.append(cur)
        elif cur is None or ln.startswith(("+++", "---")):
            continue
        elif m := _HUNK.match(ln):
            line_no = int(m.group(1))
        elif ln.startswith("+"):
            cur.added.append((line_no, ln[1:]))
            line_no += 1
        elif ln.startswith("-"):
            cur.removed.append(ln[1:])
        elif not ln.startswith("\\"):
            line_no += 1
    return files


def strip_secrets(patch: str) -> tuple[str, list[str]]:
    kept, skipped, skip = [], [], False
    for ln in patch.splitlines(keepends=True):
        if ln.startswith("diff --git "):
            path = ln.rstrip("\n").split(" b/", 1)[-1]
            skip = is_secret_path(path)
            if skip:
                skipped.append(path)
        if not skip:
            kept.append(ln)
    return "".join(kept), skipped

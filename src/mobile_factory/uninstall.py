from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .adapters import BLOCK_END, BLOCK_START
from .errors import ConfigError
from .init import GITIGNORE
from .proc import run
from .state import RunStore

EXCLUDE_LINES = {
    "/factory.yaml",
    "/.factory/",
    "/.factory/local.yaml",
    "/.mcp.json",
    "/.cursor/",
    "/.claude/skills/factory/",
    "/.claude/skills/factory-*/",
    "/CLAUDE.md",
    "/AGENTS.md",
}


@dataclass
class Removal:
    delete: list[Path] = field(default_factory=list)  # files and folders removed whole
    edit: list[str] = field(default_factory=list)  # human-readable edits to files that stay
    open_runs: list[str] = field(default_factory=list)
    tracked: list[str] = field(default_factory=list)  # deletions git will show

    def render(self, root: Path) -> str:
        lines = [f"delete  {p.relative_to(root)}{'/' if p.is_dir() else ''}" for p in self.delete]
        lines += [f"edit    {e}" for e in self.edit]
        if self.tracked:
            lines.append(f"note    committed files will show as deleted in git: {', '.join(self.tracked)}")
        return "\n".join(lines) or "nothing to remove: no factory files in this repo"


def _mcp_names(root: Path) -> set[str]:
    try:
        raw = config.read_yaml(root / config.CONFIG_NAME)
    except ConfigError:
        return set()
    return set((raw.get("mcp_servers") or {}).keys())


def _strip_block(text: str) -> str:
    return re.sub(
        r"\n*" + re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", "\n", text, flags=re.S
    ).strip()


def _exclude_file(root: Path) -> Path | None:
    r = run(["git", "rev-parse", "--git-path", "info/exclude"], root)
    if not r.ok:
        return None
    p = Path(r.out.strip())
    return p if p.is_absolute() else root / p


def plan(root: Path) -> Removal:
    rm = Removal()
    for rel in (config.CONFIG_NAME, ".factory", ".claude/skills/factory"):
        if (root / rel).exists():
            rm.delete.append(root / rel)
    rm.delete += sorted((root / ".claude/skills").glob("factory-*")) + sorted(
        (root / ".cursor/rules").glob("factory*.mdc")
    )

    names = _mcp_names(root)
    for rel in (".mcp.json", ".cursor/mcp.json"):
        f = root / rel
        if f.is_file() and names:
            ours = sorted(names & set(json.loads(f.read_text()).get("mcpServers", {})))
            if ours:
                rm.edit.append(f"{rel}: remove MCP servers {', '.join(ours)}")
    for rel in ("CLAUDE.md", "AGENTS.md"):
        f = root / rel
        if f.is_file() and BLOCK_START in f.read_text():
            rm.edit.append(f"{rel}: remove the Mobile Factory block")
    gi = root / ".gitignore"
    if gi.is_file() and any(ln in gi.read_text().splitlines() for ln in [*GITIGNORE, "# mobile-factory"]):
        rm.edit.append(".gitignore: remove the mobile-factory lines")
    ex = _exclude_file(root)
    if ex and ex.is_file() and EXCLUDE_LINES & set(ex.read_text().splitlines()):
        rm.edit.append(".git/info/exclude: remove the mobile-factory lines")

    rm.open_runs = [r.id for r in RunStore(root / ".factory" / "runs").all() if not r.finished]
    if rm.delete:  # `git ls-files` without paths would list the whole repo
        r = run(["git", "ls-files", "--", *(str(p.relative_to(root)) for p in rm.delete)], root)
        top = {ln.split("/")[0] if ln.startswith(".factory/") else ln for ln in r.out.split()} if r.ok else set()
        rm.tracked = sorted(top)
    return rm


def apply(root: Path, rm: Removal) -> None:
    names = _mcp_names(root)  # read before factory.yaml goes away
    for rel in (".mcp.json", ".cursor/mcp.json"):
        f = root / rel
        if f.is_file() and names:
            data = json.loads(f.read_text())
            servers = {k: v for k, v in data.get("mcpServers", {}).items() if k not in names}
            if servers or set(data) - {"mcpServers"}:
                f.write_text(json.dumps({**data, "mcpServers": servers}, indent=2) + "\n")
            else:
                f.unlink()
    for rel in ("CLAUDE.md", "AGENTS.md"):
        f = root / rel
        if f.is_file() and BLOCK_START in f.read_text():
            rest = _strip_block(f.read_text())
            if rest:
                f.write_text(rest + "\n")
            else:
                f.unlink()
    gi = root / ".gitignore"
    if gi.is_file():
        drop = {*GITIGNORE, "# mobile-factory"}
        kept = [ln for ln in gi.read_text().splitlines() if ln not in drop]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"
        head = run(["git", "show", "HEAD:.gitignore"], root)
        if head.ok and head.out.strip() == text.strip():
            text = head.out  # back to the committed bytes, trailing newline or not
        gi.write_text(text)
    ex = _exclude_file(root)
    if ex and ex.is_file():
        ex.write_text("\n".join(ln for ln in ex.read_text().splitlines() if ln not in EXCLUDE_LINES) + "\n")
    for p in rm.delete:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    for d in (root / ".claude/skills", root / ".claude", root / ".cursor/rules", root / ".cursor"):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

from __future__ import annotations

import re
from pathlib import Path

from .errors import FactoryError
from .globs import matches
from .proc import out, run

# Files the factory itself writes into a repo: never part of a fix, never "dirty".
FACTORY_OWNED = [
    "factory.yaml",
    ".factory/**",
    ".claude/skills/factory/**",
    ".claude/skills/factory-*/**",
    ".claude/agents/factory-*.md",
    ".claude/settings.local.json",
    ".cursor/rules/factory*.mdc",
    ".cursor/agents/factory-*.md",
    ".cursor/mcp.json",
    ".mcp.json",
    "CLAUDE.md",
    "AGENTS.md",
]


class Git:
    def __init__(self, root: Path, local_only: list[str] | None = None) -> None:
        self.root = root
        self.local_only = local_only or []

    def __call__(self, *args: str, check: bool = True) -> str:
        return out(["git", *args], self.root) if check else run(["git", *args], self.root).out.strip()

    def head(self) -> str:
        return self("rev-parse", "HEAD")

    def branch(self) -> str:
        return self("rev-parse", "--abbrev-ref", "HEAD")

    def remote_repo(self, remote: str = "origin") -> str:
        url = self("remote", "get-url", remote)
        return re.sub(r"\.git$", "", re.sub(r"^(git@|ssh://git@|https://)[^/:]+[:/]", "", url))

    def _keep(self, path: str) -> bool:
        return bool(path) and not matches(path, FACTORY_OWNED) and not matches(path, self.local_only)

    def dirty(self) -> list[str]:
        lines = self("status", "--porcelain", "--untracked-files=all", check=False).splitlines()
        return [ln[3:] for ln in lines if self._keep(ln[3:].split(" -> ")[-1])]

    def fetch(self, remote: str, branch: str) -> None:
        run(["git", "fetch", "--quiet", remote, branch], self.root, check=True)

    def merge_base(self, base_ref: str) -> str:
        return self("merge-base", "HEAD", base_ref)

    def changed_files(self, since: str) -> list[str]:
        committed = self("diff", "--name-only", since, "HEAD").splitlines()
        worktree = self("diff", "--name-only", "HEAD").splitlines()
        untracked = self("ls-files", "--others", "--exclude-standard").splitlines()
        return sorted({f for f in committed + worktree + untracked if self._keep(f)})

    def deleted_files(self, since: str) -> list[str]:
        return [f for f in self("diff", "--diff-filter=D", "--name-only", since).splitlines() if self._keep(f)]

    def diff(self, since: str, files: list[str] | None = None) -> str:
        paths = files if files is not None else self.changed_files(since)
        if not paths:
            return ""
        untracked = set(self("ls-files", "--others", "--exclude-standard").splitlines())
        tracked = [f for f in paths if f not in untracked]
        text = self("diff", since, "--", *tracked) + "\n" if tracked else ""
        for f in paths:
            if f in untracked:
                text += run(["git", "diff", "--no-index", "/dev/null", f], self.root).out
        return text

    def numstat(self, since: str) -> int:
        total = 0
        for line in self("diff", "--numstat", since, check=False).splitlines():
            a, d, *_ = line.split("\t")
            total += int(a) if a.isdigit() else 0
            total += int(d) if d.isdigit() else 0
        return total

    def create_branch(self, name: str, base_ref: str) -> None:
        if self("branch", "--list", name):
            raise FactoryError(f"branch {name} already exists: delete it or resume the run that created it")
        self("switch", "-c", name, base_ref)

    def commit(self, files: list[str], message: str, amend: bool = False) -> str:
        present = [f for f in files if (self.root / f).exists()]
        gone = [f for f in files if not (self.root / f).exists()]
        if present:
            self("add", "--", *present)
        if gone:
            self("rm", "--cached", "--quiet", "--ignore-unmatch", "--", *gone)
        self("commit", "--quiet", *(["--amend", "--no-edit"] if amend else ["-m", message]))
        return self.head()

    def save_patch(self, since: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.diff(since))

    def rollback(self, checkpoint: str) -> list[str]:
        """Restore every file touched since `checkpoint`; local-only and .factory files are left alone."""
        files = self.changed_files(checkpoint)
        if self.head() != checkpoint:
            self("reset", "--quiet", "--soft", checkpoint)
            self("reset", "--quiet")
        existed = set(self("ls-tree", "-r", "--name-only", checkpoint).splitlines())
        restore = [f for f in files if f in existed]
        if restore:
            self("checkout", checkpoint, "--", *restore)
        for f in files:
            if f not in existed:
                (self.root / f).unlink(missing_ok=True)
        return files

    def commits_since(self, base: str) -> int:
        return int(self("rev-list", "--count", f"{base}..HEAD"))

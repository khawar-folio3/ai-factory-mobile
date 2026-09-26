from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import FactoryError


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    check: bool = False,
    log: Path | None = None,
    timeout: float | None = None,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> Result:
    exe = which(cmd[0])
    if not exe and not Path(cmd[0]).exists():
        raise FactoryError(f"`{cmd[0]}` not found on PATH")
    cmd = [exe or cmd[0], *cmd[1:]]
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input, env=env, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise FactoryError(f"`{' '.join(cmd[:3])}` timed out after {timeout}s") from e
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"$ {' '.join(cmd)}\n{p.stdout}\n--- stderr ---\n{p.stderr}")
    r = Result(p.returncode, p.stdout, p.stderr)
    if check and not r.ok:
        tail = (p.stderr or p.stdout).strip().splitlines()[-8:]
        raise FactoryError(f"`{' '.join(cmd[:4])}` failed ({p.returncode}):\n" + "\n".join(tail))
    return r


def out(cmd: list[str], cwd: Path | None = None) -> str:
    return run(cmd, cwd, check=True).out.strip()


LOCAL_BIN = Path.home() / ".local" / "bin"  # where the Claude Code, Cursor CLI and factory installers put binaries


def which(tool: str) -> str | None:
    """PATH first, then ~/.local/bin: a CLI installed a moment ago may not be on this shell's PATH yet."""
    return shutil.which(tool) or shutil.which(tool, path=str(LOCAL_BIN))


def has(tool: str) -> bool:
    return which(tool) is not None

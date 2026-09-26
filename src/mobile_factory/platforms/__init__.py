from __future__ import annotations

from ..config import LoadedConfig
from .android import Android
from .base import Platform


def make(lc: LoadedConfig) -> Platform:
    if lc.cfg.project.platform == "android":
        return Android(lc.root, lc.cfg.android)
    raise NotImplementedError(f"platform {lc.cfg.project.platform}")

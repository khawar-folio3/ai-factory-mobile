from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_to_re(glob: str) -> re.Pattern[str]:
    """`**/` = any depth (including none), `**` = anything, `*` = within one segment, `?` = one char."""
    if glob == "*":
        glob = "**"
    out, i = ["^"], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches(path: str, globs: Iterable[str]) -> bool:
    return any(glob_to_re(g.strip()).match(path) for g in globs if g.strip())

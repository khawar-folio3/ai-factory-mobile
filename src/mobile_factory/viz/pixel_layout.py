from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..proc import which

WALL, VOID = 0, 255
MARK = "mobile-factory"  # layouts we wrote carry this; any other layout.json is the developer's own
OLD_AREA = "Factory"  # a wing area of earlier versions; its folder mappings are removed

# One open room of desk pods: a desk with a PC and three benches facing it. A session takes one bench; Pixel Agents
# never seats subagents but drops each on the free walkable tile nearest its parent, and free seats are walkable,
# so a session's subagents land on the benches beside it. Pods are 3 tiles apart so neighbours never mix.
PODS_ACROSS, PODS_DOWN = 4, 3
POD_W, POD_H, GAP = 3, 3, 3  # desk 3x2 + bench row; walkway between pods
MARGIN = 2  # floor between the walls and the outer pods
WALL_DECOR = ("SMALL_PAINTING", "CLOCK", "LARGE_PAINTING", "DOUBLE_BOOKSHELF", "SMALL_PAINTING_2")


def default_layout() -> dict[str, Any] | None:
    """The office's own default layout, from the installed pixel-agents package (for its floor and wall styles)."""
    exe = which("pixel-agents")
    if not exe:
        return None
    assets = Path(exe).resolve().parent / "assets"  # …/pixel-agents/dist/cli.js → dist/assets
    found = sorted(assets.glob("default-layout-*.json")) or sorted(assets.glob("default-layout.json"))
    return json.loads(found[-1].read_text()) if found else None


def open_office(base: dict[str, Any]) -> dict[str, Any]:
    """A single room of spaced-out desk pods, in the default office's floor and wall styles."""
    floor_at = next(i for i, t in enumerate(base["tiles"]) if t not in (WALL, VOID))
    wall_at = next(i for i, t in enumerate(base["tiles"]) if t == WALL)
    floor, floor_color = base["tiles"][floor_at], base["tileColors"][floor_at]
    wall_color = base["tileColors"][wall_at]

    inner_w = 2 * MARGIN + PODS_ACROSS * POD_W + (PODS_ACROSS - 1) * GAP
    inner_h = 2 * MARGIN + PODS_DOWN * POD_H + (PODS_DOWN - 1) * GAP
    top = 2  # wall row; the row above it holds wall decor, like the default office
    cols, rows = inner_w + 2, top + 1 + inner_h + 1  # side walls; a void row under the floor, as in the default
    tiles: list[int] = []
    colors: list[Any] = []
    for r in range(rows):
        for c in range(cols):
            if r < top or r == rows - 1:
                tiles.append(VOID)
                colors.append(None)
            elif r == top or c in (0, cols - 1):
                tiles.append(WALL)
                colors.append(wall_color)
            else:
                tiles.append(floor)
                colors.append(floor_color)

    furniture: list[dict[str, Any]] = []

    def put(kind: str, row: int, col: int) -> None:
        furniture.append({"uid": f"f-{MARK}-{len(furniture) + 1}", "type": kind, "col": col, "row": row})

    for down in range(PODS_DOWN):
        for across in range(PODS_ACROSS):
            col = 1 + MARGIN + across * (POD_W + GAP)
            row = top + 1 + MARGIN + down * (POD_H + GAP)
            put("DESK_FRONT", row, col)
            put("PC_FRONT_OFF", row, col + 1)
            for seat in range(POD_W):  # three benches facing the desk
                put("CUSHIONED_BENCH", row + 2, col + seat)
    step = (cols - 4) // (len(WALL_DECOR) - 1)
    for i, kind in enumerate(WALL_DECOR):
        put(kind, top - 1, 2 + i * step)
    for row, col in ((top + 1, 1), (top + 1, cols - 2), (rows - 2, 1), (rows - 2, cols - 2)):
        put("PLANT", row, col)

    return {
        "version": base.get("version", 1),
        "cols": cols,
        "rows": rows,
        "layoutRevision": base.get("layoutRevision", 1),
        "tiles": tiles,
        "tileColors": colors,
        "furniture": furniture,
        "createdBy": MARK,
    }


def ensure_layout(home: Path) -> str:
    """Write the open office unless the developer has their own layout. A layout is ours only while it is
    byte-for-byte what we last wrote (its hash is kept next to it); any edit in the office makes it theirs."""
    f, stamp = home / "layout.json", home / "layout.mobile-factory.sha256"
    if f.is_file() and not (stamp.is_file() and stamp.read_text().strip() == _sha(f.read_bytes())):
        return "kept"
    base = default_layout()
    if not base:
        return "no-default"
    data = (json.dumps(open_office(base), indent=2) + "\n").encode()
    if f.is_file() and f.read_bytes() == data:
        return "current"
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.factory-tmp")
    tmp.write_bytes(data)
    tmp.replace(f)
    stamp.write_text(_sha(data) + "\n")
    return "written"


def drop_old_mappings(home: Path) -> None:
    """Earlier versions mapped repo folders to a wing area; the open office needs no areas."""
    cfg_file = home / "config.json"
    if not cfg_file.is_file():
        return
    cfg = json.loads(cfg_file.read_text())
    mappings = cfg.get("standalone", {}).get("areaMappings", {})
    stale = [k for k, v in mappings.items() if v == [OLD_AREA]]
    if stale:
        for k in stale:
            del mappings[k]
        tmp = cfg_file.with_suffix(".json.factory-tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        tmp.replace(cfg_file)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

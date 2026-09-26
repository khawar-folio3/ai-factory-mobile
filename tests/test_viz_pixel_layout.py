from __future__ import annotations

import json
from pathlib import Path

import pytest

from mobile_factory.viz import pixel_layout as office
from mobile_factory.viz.pixel import PixelAgents


def _base() -> dict[str, object]:
    """A tiny default office: just enough for its floor and wall styles."""
    tiles = [office.VOID, office.WALL, 7, 7]
    return {
        "version": 1,
        "cols": 2,
        "rows": 2,
        "tiles": tiles,
        "tileColors": [None, {"w": 1}, {"f": 1}, {"f": 1}],
        "furniture": [],
    }


def test_open_office_is_one_room_of_spaced_three_seat_pods() -> None:
    layout = office.open_office(_base())
    cols, rows, tiles = layout["cols"], layout["rows"], layout["tiles"]
    assert len(tiles) == len(layout["tileColors"]) == cols * rows
    assert "areas" not in layout and "areaTiles" not in layout  # no extra seating logic
    kinds = [f["type"] for f in layout["furniture"]]
    pods = office.PODS_ACROSS * office.PODS_DOWN
    assert kinds.count("DESK_FRONT") == kinds.count("PC_FRONT_OFF") == pods
    assert kinds.count("CUSHIONED_BENCH") == 3 * pods
    # one room: every floor tile is inside the outer walls, with no inner wall
    floor_rows = [
        r for r in range(rows) if any(tiles[r * cols + c] not in (office.WALL, office.VOID) for c in range(cols))
    ]
    for r in floor_rows:
        row = tiles[r * cols : (r + 1) * cols]
        assert row[0] == row[-1] == office.WALL and office.WALL not in row[1:-1]
    # every bench sits on floor, right below its desk's lower row, next to its pod-mates
    benches = {(f["row"], f["col"]) for f in layout["furniture"] if f["type"] == "CUSHIONED_BENCH"}
    desks = [(f["row"], f["col"]) for f in layout["furniture"] if f["type"] == "DESK_FRONT"]
    for r, c in desks:
        assert {(r + 2, c), (r + 2, c + 1), (r + 2, c + 2)} <= benches
    assert all(tiles[r * cols + c] not in (office.WALL, office.VOID) for r, c in benches)


def test_own_layout_is_never_replaced_but_ours_is_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    home: Path = PixelAgents.home
    monkeypatch.setattr(office, "default_layout", _base)
    assert office.ensure_layout(home) == "written"
    assert json.loads((home / "layout.json").read_text())["createdBy"] == office.MARK
    assert office.ensure_layout(home) == "current"  # untouched and up to date
    layout = json.loads((home / "layout.json").read_text())
    (home / "layout.json").write_text(json.dumps({**layout, "cols": 3}))  # edited in the office, marker kept
    assert office.ensure_layout(home) == "kept"
    (home / "layout.json").write_text(json.dumps({"cols": 3, "rows": 3}))  # the developer's own layout
    assert office.ensure_layout(home) == "kept"
    assert json.loads((home / "layout.json").read_text()) == {"cols": 3, "rows": 3}


def test_old_wing_mappings_are_dropped() -> None:
    home: Path = PixelAgents.home
    (home / "config.json").write_text(
        json.dumps({"standalone": {"areaMappings": {"demo": [office.OLD_AREA], "other": ["X"]}}})
    )
    office.drop_old_mappings(home)
    assert json.loads((home / "config.json").read_text())["standalone"]["areaMappings"] == {"other": ["X"]}

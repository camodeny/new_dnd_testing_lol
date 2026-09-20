"""Deterministic grid geometry + reachable-space service — issue #232.

Pure functions only: no DB, no models, no randomness. Every movement-legality
decision in ``maps.py`` funnels through this module so geometry, path cost,
and diagonal/corner behavior stay code-owned and auditable. The AI never
invents positions or costs — it chooses only among cells this module proves
reachable.

Rules (launch policy, rule-configurable per map):
- 8-neighborhood movement on a rectangular grid; one square costs 5 ft.
- ``no_corner_cut`` (default): a diagonal step is illegal when either
  orthogonally adjacent corner cell is blocked (a token cannot squeeze past
  a wall corner). ``allow_corner_cut`` permits it.
- Blocked cells deny entry; difficult cells multiply entry cost (baseline x2,
  per-zone multiplier supported). Later zones win on overlap so DM edits can
  re-carve earlier terrain.
- Dijkstra over entry costs yields cheapest-cost reachable space; paths are
  reconstructed deterministically (neighbor order fixed, ties broken by
  coordinate) so identical inputs always produce identical paths.
"""

from __future__ import annotations

import heapq
from typing import Iterable, Mapping

FEET_PER_SQUARE = 5

NO_CORNER_CUT = "no_corner_cut"
ALLOW_CORNER_CUT = "allow_corner_cut"
DIAGONAL_POLICIES = (NO_CORNER_CUT, ALLOW_CORNER_CUT)

MAX_GRID_DIMENSION = 100

# Fixed neighbor exploration order: deterministic Dijkstra tie-breaking so
# equal-cost paths resolve identically on every run and every host.
DIRECTIONS: tuple[tuple[int, int], ...] = (
    (0, -1), (1, -1), (1, 0), (1, 1),
    (0, 1), (-1, 1), (-1, 0), (-1, -1),
)


class GeometryError(ValueError):
    """Deterministic geometry validation failure — caller must reject, never guess."""


def validate_dimensions(width: int, height: int) -> tuple[int, int]:
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError) as exc:
        raise GeometryError("map width and height must be integers") from exc
    if not 1 <= width <= MAX_GRID_DIMENSION or not 1 <= height <= MAX_GRID_DIMENSION:
        raise GeometryError(
            f"map dimensions must be between 1 and {MAX_GRID_DIMENSION} squares per side"
        )
    return width, height


def validate_diagonal_policy(policy: str) -> str:
    policy = str(policy or "").strip()
    if policy not in DIAGONAL_POLICIES:
        raise GeometryError(f"diagonal_policy must be one of {list(DIAGONAL_POLICIES)}")
    return policy


def validate_cell(width: int, height: int, col: int, row: int, *, label: str = "cell") -> tuple[int, int]:
    try:
        col, row = int(col), int(row)
    except (TypeError, ValueError) as exc:
        raise GeometryError(f"{label} coordinates must be integers") from exc
    if not (0 <= col < width and 0 <= row < height):
        raise GeometryError(f"{label} ({col}, {row}) is out of bounds for a {width}x{height} map")
    return col, row


def validate_rect(width: int, height: int, rect: Mapping) -> tuple[int, int, int, int]:
    """Validate a terrain rectangle is well-formed and fully inside the grid."""
    if not isinstance(rect, Mapping):
        raise GeometryError("terrain rect must be an object with col/row/width/height")
    try:
        col, row = int(rect["col"]), int(rect["row"])
        rect_width, rect_height = int(rect["width"]), int(rect["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeometryError("terrain rect requires integer col/row/width/height") from exc
    if rect_width < 1 or rect_height < 1:
        raise GeometryError("terrain rect width and height must be at least 1")
    if rect_width > MAX_GRID_DIMENSION or rect_height > MAX_GRID_DIMENSION:
        raise GeometryError("terrain rect exceeds the maximum grid dimension")
    if not (0 <= col and 0 <= row and col + rect_width <= width and row + rect_height <= height):
        raise GeometryError("terrain rect must fit entirely inside the map bounds")
    return col, row, rect_width, rect_height


def zone_cell_effect(zones: Iterable[Mapping]) -> dict[tuple[int, int], dict]:
    """Fold an ordered zone list into per-cell movement profiles.

    Later zones win on overlap (DM re-carve semantics). Returns a sparse map
    of ``(col, row) -> {"blocked": bool, "cost": int}`` covering only cells
    touched by at least one non-open zone; untouched cells cost 1.
    """
    profiles: dict[tuple[int, int], dict] = {}
    for zone in zones or []:
        if not isinstance(zone, Mapping):
            continue
        kind = str(zone.get("kind") or "").strip().lower()
        rect = zone.get("rect") or {}
        try:
            col, row = int(rect["col"]), int(rect["row"])
            rect_width, rect_height = int(rect["width"]), int(rect["height"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            multiplier = int(zone.get("cost_multiplier", 2))
        except (TypeError, ValueError):
            multiplier = 2
        multiplier = max(1, min(10, multiplier))
        for dy in range(rect_height):
            for dx in range(rect_width):
                key = (col + dx, row + dy)
                if kind == "blocked":
                    profiles[key] = {"blocked": True, "cost": 1}
                elif kind == "difficult":
                    profiles[key] = {"blocked": False, "cost": multiplier}
                elif kind == "open":
                    # Explicit clearing: removes any earlier zone effect.
                    profiles.pop(key, None)
    return profiles


def cell_profile(profiles: Mapping[tuple[int, int], dict], col: int, row: int) -> dict:
    entry = profiles.get((col, row))
    if entry is None:
        return {"blocked": False, "cost": 1}
    return {"blocked": bool(entry.get("blocked", False)), "cost": max(1, int(entry.get("cost", 1)))}


def _diagonal_blocked(
    profiles: Mapping[tuple[int, int], dict],
    width: int,
    height: int,
    col: int,
    row: int,
    dx: int,
    dy: int,
) -> bool:
    """True when a diagonal step from (col,row) cuts a blocked corner."""
    for corner in ((col + dx, row), (col, row + dy)):
        cx, cy = corner
        if 0 <= cx < width and 0 <= cy < height and cell_profile(profiles, cx, cy)["blocked"]:
            return True
    return False


def _dijkstra(
    *,
    width: int,
    height: int,
    profiles: Mapping[tuple[int, int], dict],
    start: tuple[int, int],
    max_squares: int,
    diagonal_policy: str,
) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], tuple[int, int]]]:
    distances: dict[tuple[int, int], int] = {start: 0}
    predecessors: dict[tuple[int, int], tuple[int, int]] = {}
    queue: list[tuple[int, int, int]] = [(0, start[0], start[1])]
    while queue:
        spent, col, row = heapq.heappop(queue)
        if spent != distances.get((col, row)):
            continue
        for dx, dy in DIRECTIONS:
            next_col, next_row = col + dx, row + dy
            if not (0 <= next_col < width and 0 <= next_row < height):
                continue
            if cell_profile(profiles, next_col, next_row)["blocked"]:
                continue
            if (
                dx != 0
                and dy != 0
                and diagonal_policy == NO_CORNER_CUT
                and _diagonal_blocked(profiles, width, height, col, row, dx, dy)
            ):
                continue
            next_spent = spent + cell_profile(profiles, next_col, next_row)["cost"]
            key = (next_col, next_row)
            if next_spent > max_squares or next_spent >= distances.get(key, max_squares + 1):
                continue
            distances[key] = next_spent
            predecessors[key] = (col, row)
            heapq.heappush(queue, (next_spent, next_col, next_row))
    return distances, predecessors


def reachable_cells(
    *,
    width: int,
    height: int,
    zones: Iterable[Mapping],
    start: tuple[int, int],
    max_squares: int,
    diagonal_policy: str = NO_CORNER_CUT,
) -> dict[tuple[int, int], int]:
    """Cheapest entry-cost (in squares) for every cell reachable within budget.

    Includes the start cell at cost 0. Blocked and out-of-budget cells are
    absent. Raises GeometryError on invalid geometry; never returns partial
    results on failure (callers commit nothing when this raises).
    """
    width, height = validate_dimensions(width, height)
    diagonal_policy = validate_diagonal_policy(diagonal_policy)
    start_col, start_row = validate_cell(width, height, start[0], start[1], label="start")
    try:
        max_squares = int(max_squares)
    except (TypeError, ValueError) as exc:
        raise GeometryError("max_squares must be an integer") from exc
    if max_squares < 0:
        raise GeometryError("max_squares cannot be negative")

    profiles = zone_cell_effect(zones)
    # A blocked start never denies exit: a token stranded by a later DM
    # terrain change can still move out (destinations stay denied below).
    # Seeding the start at cost 0 regardless keeps escape routes computable
    # while entry into blocked cells remains impossible.
    distances, _ = _dijkstra(
        width=width,
        height=height,
        profiles=profiles,
        start=(start_col, start_row),
        max_squares=max_squares,
        diagonal_policy=diagonal_policy,
    )
    return distances


def cheapest_path(
    *,
    width: int,
    height: int,
    zones: Iterable[Mapping],
    start: tuple[int, int],
    goal: tuple[int, int],
    max_squares: int,
    diagonal_policy: str = NO_CORNER_CUT,
) -> tuple[list[tuple[int, int]], int]:
    """Cheapest path from start to goal within budget, or raise GeometryError.

    Returns (path cells inclusive of both endpoints, cost in squares).
    Rejection reasons distinguish out-of-bounds, blocked, unreachable, and
    insufficient-movement so callers can report exact failure causes.
    """
    width, height = validate_dimensions(width, height)
    start_col, start_row = validate_cell(width, height, start[0], start[1], label="start")
    try:
        goal_col, goal_row = int(goal[0]), int(goal[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise GeometryError("destination coordinates must be integers") from exc
    if not (0 <= goal_col < width and 0 <= goal_row < height):
        raise GeometryError(
            f"destination ({goal_col}, {goal_row}) is out of bounds for a {width}x{height} map",
        )
    try:
        max_squares = int(max_squares)
    except (TypeError, ValueError) as exc:
        raise GeometryError("max_squares must be an integer") from exc
    if max_squares < 0:
        raise GeometryError("max_squares cannot be negative")

    distances, predecessors = _dijkstra(
        width=width,
        height=height,
        profiles=zone_cell_effect(zones),
        start=(start_col, start_row),
        max_squares=max_squares,
        diagonal_policy=validate_diagonal_policy(diagonal_policy),
    )
    key = (goal_col, goal_row)
    if key == (start_col, start_row):
        return [(start_col, start_row)], 0
    if key not in distances:
        profiles = zone_cell_effect(zones)
        if cell_profile(profiles, goal_col, goal_row)["blocked"]:
            raise GeometryError(f"destination ({goal_col}, {goal_row}) is blocked by terrain")
        raise GeometryError(
            f"destination ({goal_col}, {goal_row}) is unreachable within {max_squares} squares "
            "of movement"
        )
    path = [key]
    cursor = key
    while cursor != (start_col, start_row):
        cursor = predecessors[cursor]
        path.append(cursor)
    path.reverse()
    return path, distances[key]


def feet_to_squares(feet: int) -> int:
    return max(0, int(feet) // FEET_PER_SQUARE)


def squares_to_feet(squares: int) -> int:
    return max(0, int(squares)) * FEET_PER_SQUARE

"""Walkable floor grid and shortest paths to the exits."""

import heapq
import math
from dataclasses import dataclass

from firesim.fire.physics import distance_to_object
from firesim.schema import Opening, Room, RoomObject, inside_floor

CELL = 0.1  # m
STEP_OVER_HEIGHT = 0.3  # items lower than this are stepped over, slowly
OVERHEAD_CLEARANCE = 1.8  # items whose base is above this do not block
MIN_CLUTTER_HEIGHT = 0.03  # thinner than this (rugs, cables) is ignored
CLUTTER_SPEED_FACTOR = 0.5
EXIT_REACH = 0.25  # m from the doorway line that counts as being at the exit

_NEIGHBOURS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


def blocks_movement(obj: RoomObject) -> bool:
    top = obj.position.z + obj.size.height
    return obj.position.z < OVERHEAD_CLEARANCE and top > STEP_OVER_HEIGHT


def is_clutter(obj: RoomObject) -> bool:
    top = obj.position.z + obj.size.height
    return obj.position.z < 0.05 and MIN_CLUTTER_HEIGHT < top <= STEP_OVER_HEIGHT


@dataclass
class Grid:
    x0: float
    y0: float
    nx: int
    ny: int
    walkable: list[bool]  # flat, index = j * nx + i
    speed_factor: list[float]

    def centre(self, k: int) -> tuple[float, float]:
        j, i = divmod(k, self.nx)
        return self.x0 + (i + 0.5) * CELL, self.y0 + (j + 0.5) * CELL

    def cell_index(self, x: float, y: float) -> int | None:
        """The cell containing the point, walkable or not; None outside the grid."""
        i, j = math.floor((x - self.x0) / CELL), math.floor((y - self.y0) / CELL)
        return j * self.nx + i if 0 <= i < self.nx and 0 <= j < self.ny else None

    def cell_at(self, x: float, y: float) -> int | None:
        """The walkable cell containing the point, else the nearest walkable one."""
        k = self.cell_index(x, y)
        return k if k is not None and self.walkable[k] else self.nearest_walkable(x, y)

    def nearest_walkable(self, x: float, y: float) -> int | None:
        best, best_d = None, math.inf
        for k, ok in enumerate(self.walkable):
            if ok:
                cx, cy = self.centre(k)
                d = (cx - x) ** 2 + (cy - y) ** 2
                if d < best_d:
                    best, best_d = k, d
        return best

    def neighbours(self, k: int) -> list[tuple[int, float]]:
        """Walkable neighbours of a cell with step lengths. Diagonals may not cut corners."""
        j, i = divmod(k, self.nx)
        out = []
        for di, dj in _NEIGHBOURS:
            ni, nj = i + di, j + dj
            if not (0 <= ni < self.nx and 0 <= nj < self.ny):
                continue
            n = nj * self.nx + ni
            if not self.walkable[n]:
                continue
            if di and dj and not (self.walkable[j * self.nx + ni] and self.walkable[n - di]):
                continue
            out.append((n, CELL * (math.sqrt(2.0) if di and dj else 1.0)))
        return out


def build_grid(room: Room, body_radius: float) -> Grid:
    """Cells a person's centre can occupy: inside the room and clear of furniture."""
    xs, ys = [p.x for p in room.floor], [p.y for p in room.floor]
    x0, y0 = min(xs), min(ys)
    nx = math.ceil((max(xs) - x0) / CELL)
    ny = math.ceil((max(ys) - y0) / CELL)
    blockers = [o for o in room.objects if blocks_movement(o)]
    clutter = [o for o in room.objects if is_clutter(o)]

    walkable, speed = [], []
    for j in range(ny):
        for i in range(nx):
            x, y = x0 + (i + 0.5) * CELL, y0 + (j + 0.5) * CELL
            free = inside_floor(room, x, y) and all(
                distance_to_object(x, y, o.position.z, o) >= body_radius for o in blockers
            )
            cluttered = any(distance_to_object(x, y, o.position.z, o) < 0.05 for o in clutter)
            walkable.append(free)
            speed.append(CLUTTER_SPEED_FACTOR if cluttered else 1.0)
    return Grid(x0, y0, nx, ny, walkable, speed)


def opening_segment(room: Room, opening: Opening) -> tuple[float, float, float, float]:
    a = room.floor[opening.wall_index]
    b = room.floor[(opening.wall_index + 1) % len(room.floor)]
    length = math.hypot(b.x - a.x, b.y - a.y)
    ux, uy = (b.x - a.x) / length, (b.y - a.y) / length
    start, end = opening.offset, opening.offset + opening.width
    return a.x + ux * start, a.y + uy * start, a.x + ux * end, a.y + uy * end


def _distance_to_segment(x: float, y: float, segment: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = segment
    dx, dy = x2 - x1, y2 - y1
    along = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    along = min(max(along, 0.0), 1.0)
    return math.hypot(x - (x1 + along * dx), y - (y1 + along * dy))


def exit_cells(grid: Grid, room: Room, exits: list[Opening]) -> list[int]:
    segments = [opening_segment(room, o) for o in exits]
    return [
        k
        for k, ok in enumerate(grid.walkable)
        if ok and any(_distance_to_segment(*grid.centre(k), s) <= EXIT_REACH for s in segments)
    ]


def travel_costs(grid: Grid, goals: list[int], weight: list[float]) -> list[float]:
    """Dijkstra outward from the exits. weight scales the cost of entering each cell."""
    cost = [math.inf] * len(grid.walkable)
    queue = [(0.0, k) for k in goals]
    for k in goals:
        cost[k] = 0.0
    heapq.heapify(queue)
    while queue:
        c, k = heapq.heappop(queue)
        if c > cost[k]:
            continue
        for n, step in grid.neighbours(k):
            candidate = c + step * weight[k]
            if candidate < cost[n]:
                cost[n] = candidate
                heapq.heappush(queue, (candidate, n))
    return cost


def path_from(grid: Grid, cost: list[float], start: int) -> list[int]:
    """Cells from start to the nearest exit, following decreasing cost."""
    path = [start]
    while cost[path[-1]] > 0:
        options = [(cost[n], n) for n, _ in grid.neighbours(path[-1])]
        best_cost, best = min(options, default=(math.inf, -1))
        if best_cost >= cost[path[-1]]:
            break
        path.append(best)
    return path

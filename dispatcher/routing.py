"""Map and routing engine. Precomputes shortest paths between all 30 places."""
from __future__ import annotations
from typing import Optional
from spread.types import WorldMap, Place, Road

HOUR = 60
DAY = 24 * HOUR


class Router:
    """Precomputed road distances and drive times between every pair of places."""

    def __init__(self, world_map: WorldMap):
        self.places: dict[str, Place] = {p["id"]: p for p in world_map["places"]}
        self.place_ids = list(self.places.keys())
        self._idx = {pid: i for i, pid in enumerate(self.place_ids)}
        n = len(self.place_ids)

        self.minutes = [[0 if i == j else float("inf") for j in range(n)] for i in range(n)]
        self.km = [[0.0 if i == j else float("inf") for j in range(n)] for i in range(n)]

        for road in world_map["roads"]:
            i, j = self._idx[road["from"]], self._idx[road["to"]]
            self.minutes[i][j] = road["minutes"]
            self.km[i][j] = road["km"]

        # Floyd-Warshall for shortest times (the game uses road minutes for planning)
        for k in range(n):
            for i in range(n):
                for j in range(n):
                    through_k = self.minutes[i][k] + self.minutes[k][j]
                    if through_k < self.minutes[i][j]:
                        self.minutes[i][j] = through_k

    def drive_minutes(self, from_place: str, to_place: str) -> int:
        """Shortest drive time in minutes between two places."""
        i, j = self._idx[from_place], self._idx[to_place]
        return self.minutes[i][j]

    def drive_km(self, from_place: str, to_place: str) -> float:
        """Direct road distance in km (not shortest path km, just direct road)."""
        i, j = self._idx[from_place], self._idx[to_place]
        return self.km[i][j]

    def nearby_places(self, place_id: str, max_minutes: int = 60) -> list[str]:
        """Places reachable within max_minutes drive."""
        i = self._idx[place_id]
        return [pid for pid, j in self._idx.items() if self.minutes[i][j] <= max_minutes and pid != place_id]

    def can_make_deadline(self, from_place: str, to_place: str, depart_at: int, deadline: int) -> bool:
        """Can a driver leave from_place at depart_at and arrive at to_place by deadline?"""
        return depart_at + self.drive_minutes(from_place, to_place) <= deadline

    def gig_drive_hours(self, stop_places: list[str]) -> float:
        """Total driving hours for a sequence of stops. Used to check the 12-hour limit."""
        total = 0
        for i in range(len(stop_places) - 1):
            total += self.drive_minutes(stop_places[i], stop_places[i + 1])
        return total / 60.0

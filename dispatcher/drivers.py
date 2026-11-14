"""Driver intelligence. Tracks driver locations, reliability, and booking patterns."""
from __future__ import annotations
from typing import Optional
from spread.types import DriverProfile, FeeStep

HOUR = 60
DAY = 24 * HOUR


class DriverManager:
    """Tracks driver roster and behavior to optimize gig posting and offers."""

    def __init__(self):
        self.drivers: dict[str, DriverProfile] = {}
        self.driver_bookings: dict[str, int] = {}
        self.driver_drops: dict[str, int] = {}
        self.driver_last_fee: dict[str, int] = {}
        self.home_places: dict[str, list[str]] = {}
        # Direct offer tracking: driver_id -> offers made today (day number)
        self.offers_today: dict[str, int] = {}
        self.offer_day: int = -1

    def load_roster(self, roster: list[DriverProfile]) -> None:
        self.drivers = {d["id"]: d for d in roster}
        self.home_places = {}
        for d in roster:
            self.home_places.setdefault(d["homePlaceId"], []).append(d["id"])

    def drivers_near(self, place_id: str, router, max_minutes: int = 30) -> list[str]:
        result = []
        for pid, dids in self.home_places.items():
            if router.drive_minutes(pid, place_id) <= max_minutes:
                result.extend(dids)
        return result

    def supply_ratio(self, place_id: str, router, pending_orders: int = 1) -> float:
        nearby = len(self.drivers_near(place_id, router, max_minutes=45))
        if pending_orders <= 0:
            return 2.0
        return nearby / max(1, pending_orders)

    def is_experienced(self, driver_id: str) -> bool:
        d = self.drivers.get(driver_id)
        return d is not None and d["rating"] >= 4.8 and d["completedGigs"] >= 50

    def is_reliable(self, driver_id: str) -> bool:
        d = self.drivers.get(driver_id)
        if d is None:
            return False
        if d["completedGigs"] < 5:
            return True
        drop_rate = d["drops"] / max(1, d["completedGigs"])
        return drop_rate < 0.1

    def can_offer(self, driver_id: str, now: int) -> bool:
        """Can we send a direct offer to this driver? Max 3 per driver per day."""
        today = now // DAY
        if today != self.offer_day:
            self.offers_today.clear()
            self.offer_day = today
        return self.offers_today.get(driver_id, 0) < 3

    def record_offer(self, driver_id: str, now: int) -> None:
        today = now // DAY
        if today != self.offer_day:
            self.offers_today.clear()
            self.offer_day = today
        self.offers_today[driver_id] = self.offers_today.get(driver_id, 0) + 1

    def best_driver_for_offer(self, place_id: str, router, now: int,
                              need_experienced: bool = False,
                              need_manual: bool = False,
                              busy_drivers: set[str] | None = None) -> Optional[str]:
        """Pick the best available driver near a place for a direct offer."""
        candidates = self.drivers_near(place_id, router, max_minutes=45)
        busy = busy_drivers or set()

        scored: list[tuple[float, str]] = []
        for did in candidates:
            if did in busy or not self.can_offer(did, now):
                continue
            d = self.drivers[did]
            if need_experienced and not self.is_experienced(did):
                continue
            if need_manual and d["licence"] != "manual":
                continue
            # Score: prefer reliable, experienced, previously worked with us
            score = d["rating"] * 10 + d["completedGigs"] * 0.1
            if self.is_reliable(did):
                score += 20
            if did in self.driver_bookings:
                score += 10
            # Penalize distance
            travel = router.drive_minutes(d["homePlaceId"], place_id)
            score -= travel * 0.5
            scored.append((score, did))

        scored.sort(reverse=True)
        return scored[0][1] if scored else None

    def record_booking(self, driver_id: str, fee: int) -> None:
        self.driver_bookings[driver_id] = self.driver_bookings.get(driver_id, 0) + 1
        self.driver_last_fee[driver_id] = fee

    def record_drop(self, driver_id: str) -> None:
        self.driver_drops[driver_id] = self.driver_drops.get(driver_id, 0) + 1

    def fee_schedule(self, base_fee: int, now: int, level: int, urgent: bool = False) -> list[FeeStep]:
        fee = float(base_fee)
        if urgent:
            return [{"at": now + k * 30, "feeCents": max(1, int(fee * (1.5 + k / 4) + 0.5))} for k in range(1 if level == 1 else 5)]
        return [{"at": now + k * HOUR, "feeCents": max(1, int(fee * (1 + k / 10) + 0.5))} for k in range(1 if level == 1 else 6)]

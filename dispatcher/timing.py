"""Time helpers: opening hours, awake minutes and an estimate of an order's lastPickupAt."""
from __future__ import annotations
from typing import Optional

HOUR = 60
DAY = 24 * HOUR
WAKE_FROM, WAKE_TO = 7 * HOUR, 22 * HOUR  # drivers read the app from 07:00 to 23:00; we count to 22:00


def _day_hours(hours, day: int) -> Optional[tuple[int, int]]:
    """(opens, closes) in minutes of the day for world day `day`, or None when closed."""
    if hours is None:
        return (0, DAY)
    wd = day % 7
    if day >= 7:  # the last Monday and on: same weekday rules
        wd = day % 7
    if wd in hours["weekdays"]:
        return (hours["opens"], hours["closes"])
    if wd == 5 and hours.get("saturday"):
        return (hours["saturday"]["opens"], hours["saturday"]["closes"])
    return None


def is_open(hours, t: int) -> bool:
    h = _day_hours(hours, t // DAY)
    return h is not None and h[0] <= t % DAY < h[1]


def latest_open_at_or_before(hours, t: int) -> int:
    """The latest minute <= t at which the site is open (by its stated hours)."""
    if hours is None:
        return t
    day = t // DAY
    for d in range(day, day - 8, -1):
        h = _day_hours(hours, d)
        if h is None:
            continue
        start, end = d * DAY + h[0], d * DAY + h[1] - 1
        if t >= start:
            return min(t, end)
    return t - 7 * DAY


def earliest_open_at_or_after(hours, t: int) -> int:
    if hours is None:
        return t
    day = t // DAY
    for d in range(day, day + 8):
        h = _day_hours(hours, d)
        if h is None:
            continue
        start, end = d * DAY + h[0], d * DAY + h[1] - 1
        if t <= end:
            return max(t, start)
    return t + 7 * DAY


def awake_minutes(a: int, b: int) -> int:
    """Minutes between a and b that fall in drivers' waking hours."""
    tot, t = 0, a
    while t < b:
        d = t // DAY * DAY
        s, e = max(t, d + WAKE_FROM), min(b, d + WAKE_TO)
        if e > s:
            tot += e - s
        t = d + DAY
    return tot


def leg_deadline(leg, deadline: int) -> int:
    d = leg["delivery"]
    if d["privateAddress"]:
        return min(deadline, d["window"]["to"] + 30)
    return deadline


def estimate_last_pickup(legs, deadline: int, router) -> int:
    """Latest minute to collect the first car and still deliver every leg on time."""
    t = None
    for i in range(len(legs) - 1, -1, -1):
        leg = legs[i]
        dl = leg_deadline(leg, deadline)
        if t is not None:
            dl = min(dl, t)
        arrive = latest_open_at_or_before(leg["delivery"]["hours"], dl) if not leg["delivery"]["privateAddress"] else dl
        extra = 40 if (leg["car"]["electric"] and (leg["car"]["chargePercent"] or 100) < 30) else 0
        pick = arrive - router.drive_minutes(leg["pickup"]["placeId"], leg["delivery"]["placeId"]) - extra
        if leg["pickup"]["privateAddress"]:
            if i == 0:
                pick = min(pick, leg["pickup"]["window"]["to"])
        else:
            pick = latest_open_at_or_before(leg["pickup"]["hours"], pick)
        t = pick
    return t


def open_interval(hours, t: int) -> tuple[int, int]:
    """The first stretch of opening hours that ends after t: (start, end), start >= t."""
    if hours is None:
        return (t, t + 7 * DAY)
    day = t // DAY
    for d in range(day, day + 9):
        h = _day_hours(hours, d)
        if h is None:
            continue
        start, end = d * DAY + h[0], d * DAY + h[1]
        if end > t:
            return (max(t, start), end)
    return (t, t + 7 * DAY)

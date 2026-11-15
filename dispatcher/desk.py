"""Main dispatcher: the brain that ties routing, pricing, and driver management together."""
from __future__ import annotations
import collections
import json
import os
import sys
from typing import Optional
from spread.client import World
from spread.types import (
    Action, ActionResult, Audience, DeskEvent, DeskEventKind, DeskSnapshot,
    FeeStep, Gig, Note, Order, PostAction, PriceRequest, Score, StopRef, TimeWindow,
)
from spread.starter import (
    stops_for, without_windows, undelivered, pickups, retry_from, window_from_note, Retry,
)
from .routing import Router
from .pricing import Pricer
from .drivers import DriverManager
from .timing import estimate_last_pickup, awake_minutes, open_interval, latest_open_at_or_before, earliest_open_at_or_after
from .notes import read_note, noted_legs, NoteFacts

HOUR = 60
DAY = 24 * HOUR
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WAKE_ON: list[DeskEventKind] = [
    "price_request", "order_won", "order_lost", "order_changed", "order_cancelled",
    "visit_failed", "gig_expired", "deadline_close", "offer_answered",
    "driver_late", "no_show", "gig_dropped",
]

# Least awake minutes between our answer and the order's last pickup for us to take a request.
LEAD_MIN = {1: 240, 2: 240, 3: 240, 4: 240, 5: 240, 6: 240, 7: 300, 8: 300}
# Fee schedule as multiples of the typical fee: opening, level reached by the deadline warning, urgent cap.
FEE_OPEN, FEE_PLATEAU = 1.0, 1.6
# Lowball: an order with plenty of waking time opens below the typical fee and climbs to it over the hold
# phase. 32-45% of such gigs booked below the typical fee in testing; +7% margin over five level-8 weeks.
LOWBALL_OPEN, LOWBALL_MIN_SPAN = 0.80, 16 * 60  # opening fee; least waking minutes before the deadline warning
REPRICE_EVERY = 30  # level 1 has one fee step: we raise it ourselves this often
MIN_PRICE_OVER_FEE = 1.0  # levels 1-3: take a request only when its list price covers the typical fee
SAFETY_MIN, SAFETY_SHARE = 30, 0.10  # windows end this long before the last pickup and the deadline
CAP_SHARE, CAP_MIN = 0.05, 2
CAP_RICH, CAP_SHARE_RICH, CAP_POOR = 100, 0.06, 10  # places with this many drivers nearby take a larger share; below CAP_POOR one order at a time  # at most this share of the drivers near a pickup waiting on our unbooked orders there
FEE_HOLD_SHARE = 0.5  # hold the opening fee for this share of the waking time before the deadline warning
OFFER_NEAR_MIN, OFFER_RECENT = 60, 4 * HOUR  # offer a gig to our drivers who finished this close and this recently
REMOTE_SHARE = 0.015  # a delivery place with fewer than this share of all drivers nearby is remote
L8_BUFFER, L7_BUFFER = 6 * HOUR, 4 * HOUR  # levels 7-8: pickup + this + drive must fit before the deadline
MIN_DRIVERS = {7: 3, 8: 4}  # levels 7-8: least drivers living within 45 minutes of the pickup
# Adaptive guard: when our own lateness (late orders + orders past their warning without a driver)
# passes RISK_TIGHT of the orders won so far, tighten; loosen again under RISK_LOOSE.
RISK_TIGHT, RISK_LOOSE, RISK_MIN_ORDERS = 9.0, 0.02, 20  # adaptive guard off (it switched on too late to help)
TIGHT_CAP, TIGHT_STUCK, TIGHT_LEAD = 0.5, 2, 60  # when tight: cap share x0.5, stop at 2 stuck, +60 min lead
# Multi-car gigs (level 3+): chain two single-car orders when one's delivery ends near the other's pickup.
COMBO_LEVEL, COMBO_GAP, COMBO_FACTOR, COMBO_MAX_DRIVE = 99, 45, 0.90, 11 * HOUR
HOT_CAP_PLACES, HOT_CAP_SHARE = ["vantaa", "hyvinkaa"], 0.09  # level 8: a larger per-town share for our busiest pickups (0 = off)
STUCK_STOP = 3  # stop taking orders near a pickup while this many of ours there are past their warning without a driver
DECLINE = 5  # from level 4, decline with a quote this many times the list price: a lost quote costs nothing


class SmartDesk:

    def __init__(self, world: World, level: int, now: int, router: Router, pricer: Pricer, driver_mgr: DriverManager):
        self.world = world
        self.level = level
        self.now = now
        self.router = router
        self.pricer = pricer
        self.driver_mgr = driver_mgr

        self.outbox: list[Action] = []
        self.requests: dict[str, PriceRequest] = {}
        self.orders: dict[str, Order] = {}
        self.leg_gigs: dict[str, dict[int, str]] = {}
        self.booked: set[str] = set()
        self.posts: dict[str, PostAction] = {}
        self.urgent: set[str] = set()
        self.gig_posted_at: dict[str, int] = {}
        # Drivers currently working a gig (gig_booked but not yet delivered/dropped)
        self.busy_drivers: set[str] = set()
        self.released: set[str] = set()
        self.first_posted: dict[str, int] = {}  # order id -> minute of its first gig
        self.urgent_at: dict[str, int] = {}  # order id -> minute of its deadline warning
        self.gig_driver: dict[str, str] = {}
        self.dead_gigs: set[str] = set()  # gigs we released or withdrew
        self.last_offer_at: dict[str, int] = {}
        self.near_cache: dict[str, set[str]] = {}
        self.pending_pickup: dict[str, str] = {}  # quoted or won order id -> first pickup place, until booked
        self.quoted_last_pickup: dict[str, int] = {}
        self.facts: dict[str, NoteFacts] = {}
        self.reasons: collections.Counter = collections.Counter()  # why we declined requests
        self.tight = False  # the adaptive guard is on
        self.guard_log: list = []
        self.combo_count = 0
        self._risk_at = -1
        self.active: dict[str, set[str]] = {}  # driver -> our gigs they hold and have not finished
        self.last_seen: dict[str, tuple[str, int]] = {}  # driver -> (place, minute) of their last delivery for us
        self.open_offers: dict[str, int] = {}  # gig -> offers waiting for an answer
        self.offered: dict[str, set[str]] = {}  # gig -> drivers offered it

    # ── Event handling ──

    def handle(self, event: DeskEvent) -> None:
        kind = event["kind"]
        if kind == "price_request":
            self.quote(event["request"])
        elif kind == "order_won":
            self.win(event["order"])
        elif kind == "order_lost":
            self.pricer.record_loss(event["requestId"])
            self.pricer.record_result(event["requestId"], False, event["at"])
            self.pending_pickup.pop(event["requestId"], None)
        elif kind == "order_changed":
            self.change(event["order"])
        elif kind == "order_cancelled":
            self.pending_pickup.pop(event["orderId"], None)
            for books in (self.orders, self.leg_gigs):
                books.pop(event["orderId"], None)
            # A withdrawn gig that also carried another order's car: post that car again.
            for g in event.get("gigs", []):
                if g["outcome"] == "withdrawn" and self.is_combo(g["gigId"]):
                    self.dead_gigs.add(g["gigId"])
                    self.repost_others(g["gigId"], skip=event["orderId"])
        elif kind == "visit_failed":
            self.retry(event["orderId"], event["leg"], event["reason"])
        elif kind == "gig_expired":
            self.repost(event["gigId"])
        elif kind == "deadline_close":
            self.raise_fees(event["orderId"])
        elif kind == "gig_booked":
            self.booked.add(event["gigId"])
            self.gig_driver[event["gigId"]] = event["driverId"]
            self.active.setdefault(event["driverId"], set()).add(event["gigId"])
            self.busy_drivers.add(event["driverId"])
            self.driver_mgr.record_booking(event["driverId"], event["feeCents"])
        elif kind in ("gig_dropped", "no_show"):
            self.booked.discard(event["gigId"])
            self._free(event.get("driverId", ""), event["gigId"])
            if kind == "gig_dropped":
                self.driver_mgr.record_drop(event["driverId"])
        elif kind == "delivered":
            self.delivered(event["orderId"], event["leg"], event["at"])
            driver = self.gig_driver.get(event["gigId"])
            if driver:
                self.last_seen[driver] = (event["placeId"], event["at"])
                action = self.posts.get(event["gigId"])
                last = (action["stops"][-1]["orderId"], action["stops"][-1]["leg"]) if action else (event["orderId"], event["leg"])
                if (event["orderId"], event["leg"]) == last:
                    self._free(driver, event["gigId"])
        elif kind == "driver_late":
            self.driver_late(event["gigId"], event["pickupAt"])
        elif kind == "offer_answered":
            self.open_offers[event["gigId"]] = max(0, self.open_offers.get(event["gigId"], 0) - 1)
            if event["answer"] != "accepted":
                self._try_next_offer(event["gigId"])

    def _free(self, driver: str, gig_id: str) -> None:
        gigs = self.active.get(driver)
        if gigs is not None:
            gigs.discard(gig_id)
            if not gigs:
                self.busy_drivers.discard(driver)

    # ── Stricter order selection (Feature 1) ──

    def facts_of(self, request: PriceRequest) -> NoteFacts:
        f = self.facts.get(request["id"])
        if f is None:
            note = request.get("note")
            f = self.facts[request["id"]] = read_note(note["text"] if note else None, request["arrivedAt"])
        return f

    def noted_deadline(self, request: PriceRequest, deadline: int) -> int:
        """The deadline after the note: a car ready a day or more late moves it; a tighter one counts."""
        f = self.facts_of(request)
        ready = request["legs"][0]["pickup"]["window"]["from"]
        if f.pickup_from is not None and f.pickup_from // DAY > ready // DAY:
            deadline += (f.pickup_from // DAY - ready // DAY) * DAY
            while (deadline // DAY) % 7 in (5, 6):
                deadline += DAY
        if f.delivery_by is not None:
            deadline = min(deadline, f.delivery_by)
        return deadline

    def is_feasible(self, request: PriceRequest, option) -> bool:
        return self.infeasible(request, option) is None

    def infeasible(self, request: PriceRequest, option) -> Optional[str]:
        """Why we would not take this option, or None when we would."""
        deadline = self.noted_deadline(request, option["deadline"])
        # Enough waking hours for drivers to see and book the gig before its last pickup.
        legs = noted_legs(request["legs"], self.facts_of(request))
        last_pickup = estimate_last_pickup(legs, deadline, self.router)
        if last_pickup < legs[0]["pickup"]["window"]["from"]:
            return "impossible"
        lead_min = LEAD_MIN[self.level]
        longest = max(self.router.drive_minutes(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        if longest >= 4 * HOUR:
            lead_min = max(lead_min, 6 * HOUR)  # long drives with little notice were late ~40% of the time
        if self.guard():
            lead_min += TIGHT_LEAD
        if awake_minutes(request["respondBy"], last_pickup) < lead_min:
            return "lead_time"
        if not self.has_capacity(request["legs"][0]["pickup"]["placeId"]):
            return "capacity"
        if self.level < 7:
            return None
        for leg in request["legs"]:
            pickup_place = leg["pickup"]["placeId"]
            delivery_place = leg["delivery"]["placeId"]
            pickup_from = leg["pickup"]["window"]["from"]

            drive_time = self.router.drive_minutes(pickup_place, delivery_place)
            # 6h buffer at L8: drivers are scarce and slow to book
            buffer = L8_BUFFER if self.level >= 8 else L7_BUFFER
            earliest_delivery = max(self.now, pickup_from) + buffer + drive_time
            if earliest_delivery > deadline:
                return "buffer"

            nearby = self.driver_mgr.drivers_near(pickup_place, self.router, max_minutes=45)
            if len(nearby) < MIN_DRIVERS.get(self.level, 1):
                return "few_drivers"
        return None

    def has_capacity(self, place: str) -> bool:
        """Few enough of our orders near this pickup still wait for a driver."""
        nearby = self.near_cache.get(place)
        if nearby is None:
            nearby = self.near_cache[place] = {
                p for p in self.router.place_ids if self.router.drive_minutes(p, place) <= 45}
        drivers = sum(len(self.driver_mgr.home_places.get(p, [])) for p in nearby)
        # The shortage is uneven: towns that kept their drivers take more, near-empty ones less.
        share = CAP_SHARE_RICH if drivers >= CAP_RICH else CAP_SHARE
        if self.level >= 8 and place in HOT_CAP_PLACES:
            share = max(share, HOT_CAP_SHARE)
        if self.guard():
            share *= TIGHT_CAP
        cap = max(1 if drivers < CAP_POOR else CAP_MIN, int(drivers * share))
        waiting = stuck = 0
        for oid, pickup in list(self.pending_pickup.items()):
            if self.order_last_pickup(oid) < self.now - DAY:
                del self.pending_pickup[oid]
                continue
            if pickup in nearby and not any(g in self.booked for g in self.gigs_of(oid))                     and self.order_last_pickup(oid) >= self.now:
                waiting += 1
                if oid in self.urgent:
                    stuck += 1
        # Drivers there stopped booking (a flu day, a busy day): take nothing more until they catch up.
        if stuck >= (TIGHT_STUCK if self.guard() else STUCK_STOP):
            return False
        return waiting < cap

    def risk(self) -> float:
        """Our lateness so far: orders already late, or past their warning with no driver,
        over the orders we have won."""
        if len(self.orders) < RISK_MIN_ORDERS:
            return 0.0
        bad = 0
        for oid, o in self.orders.items():
            done = o["deliveredAt"]
            if all(d is not None for d in done):
                bad += max(done) > o["deadline"]
            elif self.now > o["deadline"]:
                bad += 1
            elif oid in self.urgent and not any(g in self.booked for g in self.gigs_of(oid)):
                bad += 1
        return bad / len(self.orders)

    def guard(self) -> bool:
        """Is the adaptive guard on? Re-evaluated at most once a minute of world time."""
        if self._risk_at != self.now:
            self._risk_at = self.now
            r = self.risk()
            if not self.tight and r > RISK_TIGHT:
                self.tight = True
                self.guard_log.append((self.now, "on", round(r, 3)))
            elif self.tight and r < RISK_LOOSE:
                self.tight = False
                self.guard_log.append((self.now, "off", round(r, 3)))
        return self.tight

    def order_last_pickup(self, oid: str) -> int:
        order = self.orders.get(oid)
        if order is not None:
            return order["lastPickupAt"]
        return self.quoted_last_pickup.get(oid, -1)

    # ── Quoting ──

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        self.pricer.now = self.now

        pickup_place = request["legs"][0]["pickup"]["placeId"]
        unbooked = self._unbooked_count()
        supply = self.driver_mgr.supply_ratio(pickup_place, self.router, pending_orders=max(1, unbooked))

        if self.level <= 3:
            # Every quote wins the first option at its list price; no quote costs nothing.
            first = request["options"][0]
            if not self.is_feasible(request, first):
                return
            need = MIN_PRICE_OVER_FEE * (1.15 if self.is_remote(request) else 1.0)
            if first["listPriceCents"] < request["referenceFeeCents"] * need:
                return
            self.outbox.append({"type": "quote", "requestId": request["id"],
                                "prices": {first["kind"]: first["listPriceCents"]}})
            self.note_quote(request, first["deadline"])
            return

        why = {o["kind"]: self.infeasible(request, o) for o in request["options"]}
        feasible_kinds = {k for k, r in why.items() if r is None}
        if not feasible_kinds:
            self.reasons[why[request["options"][-1]["kind"]]] += 1
            self.decline(request)
            return

        prices = self.pricer.quote_prices(request, driver_supply=supply,
                                          active_orders=len(self.orders), unbooked=unbooked)
        prices = {k: v for k, v in prices.items() if k in feasible_kinds}
        if self.is_remote(request):
            # No work comes back from there: the driver's trip home costs more, and the competitor charges more too.
            prices = {k: int(v * 1.10) for k, v in prices.items()}

        if not prices:
            self.reasons["price_margin"] += 1
            self.decline(request)
            return

        self.reasons["quoted"] += 1
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})
        self.pricer.record_quote(request)
        self.note_quote(request, max(o["deadline"] for o in request["options"] if o["kind"] in prices))

    def is_remote(self, request: PriceRequest) -> bool:
        place = request["legs"][0]["delivery"]["placeId"]
        total = max(1, len(self.driver_mgr.drivers))
        return len(self.driver_mgr.drivers_near(place, self.router, max_minutes=45)) < total * REMOTE_SHARE

    def note_quote(self, request: PriceRequest, deadline: int) -> None:
        """Count a quote as an order waiting for a driver until the dealer decides."""
        self.pending_pickup[request["id"]] = request["legs"][0]["pickup"]["placeId"]
        self.quoted_last_pickup[request["id"]] = estimate_last_pickup(request["legs"], deadline, self.router)

    def decline(self, request: PriceRequest) -> None:
        first = request["options"][0]
        self.outbox.append({"type": "quote", "requestId": request["id"],
                            "prices": {first["kind"]: first["listPriceCents"] * DECLINE}})

    # ── Order management ──

    def win(self, order: Order) -> None:
        if order["id"] in self.requests and order["id"] not in self.orders:
            self.orders[order["id"]] = order
            self.pricer.record_win(order["dealerId"], order["priceCents"], order["option"])
            self.pricer.record_result(order["id"], True, self.now)
            if self.level >= COMBO_LEVEL and self.try_combo(order):
                return
            self.post(order, undelivered(order))

    def change(self, order: Order) -> None:
        if order["id"] in self.orders:
            self.orders[order["id"]] = order
            if self.withdraw(order["id"]):
                self.post(order, undelivered(order))

    def delivered(self, order_id: str, leg: int, at: int) -> None:
        self.leg_gigs.get(order_id, {}).pop(leg, None)
        order = self.orders.get(order_id)
        if order is not None:
            order["deliveredAt"][leg] = at
            if all(d is not None for d in order["deliveredAt"]):
                self.pending_pickup.pop(order_id, None)
                on_time = max(order["deliveredAt"]) <= order["deadline"]
                self.pricer.record_delivery(order["dealerId"], on_time)

    # ── Gig posting ──

    def eff_last_pickup(self, order: Order) -> int:
        """The last pickup that keeps the order on time, earlier for a private seller's slot."""
        lp = order["lastPickupAt"]
        left = undelivered(order)
        if left:
            first = order["legs"][left[0]]["pickup"]
            if first["privateAddress"]:
                lp = min(lp, first["window"]["to"])
        return lp

    def post(self, order: Order, legs: list[int], retry: Retry = Retry()) -> None:
        request = self.requests[order["id"]]
        facts = self.facts_of(request)
        audience: Audience = {
            "experienced": any(order["legs"][l]["car"]["premium"] for l in legs),
            "manualLicence": retry.manual_licence or facts.manual,
        }
        late = self.now >= self.eff_last_pickup(order) - 15
        stops = self.tighten(order, stops_for(order, legs, None, retry), facts, late)
        stops = without_windows(stops, self.now)
        if late and order["id"] not in self.urgent:
            self.urgent.add(order["id"])
            self.urgent_at[order["id"]] = self.now
        schedule = self.schedule_for(order, len(legs))

        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": schedule})

        if len(legs) == 1:
            return
        self.send()
        if not self.gigs_of(order["id"]):
            for leg in legs:
                self.post(order, [leg], retry)

    def tighten(self, order: Order, stops: list[StopRef], facts: NoteFacts, late: bool = False) -> list[StopRef]:
        """Give the driver the windows that keep the order on time: the hours and ready time from
        the note, a private customer's booked hour, and a margin before the last pickup and the
        deadline, so a driver does not plan to arrive in the last minutes. A late car keeps only
        what makes the visit possible: the hours and ready time."""
        legs = noted_legs(order["legs"], facts)
        out: list[StopRef] = []
        first_pickup = True
        for stop in stops:
            leg = legs[stop["leg"]]
            place = leg[stop["kind"]]
            base = order["legs"][stop["leg"]][stop["kind"]]["window"]
            own = stop.get("window")
            frm = max(own["from"] if own else base["from"], base["from"])
            to = own["to"] if own else None
            drive = self.router.drive_minutes(leg["pickup"]["placeId"], leg["delivery"]["placeId"])
            buffer = SAFETY_MIN + int(drive * SAFETY_SHARE)
            if stop["kind"] == "pickup" and stop["leg"] == 0 and facts.plates:
                stop = {**stop, "withPlates": True}
            if place["privateAddress"]:
                to = min(to, base["to"]) if to is not None else base["to"]
            elif stop["kind"] == "pickup":
                if stop["leg"] == 0 and facts.pickup_from is not None:
                    frm = max(frm, facts.pickup_from)
                if place["hours"] is not None:
                    o, c = open_interval(place["hours"], max(frm, self.now))
                    frm = max(frm, o)
                    to = min(to, c) if to is not None else c
                if first_pickup and not late:
                    cap = order["lastPickupAt"] - buffer
                    if cap > frm:
                        to = min(to, cap) if to is not None else cap
            elif stop["kind"] == "delivery" and not late:
                cap = order["deadline"] - buffer
                if facts.delivery_by is not None:
                    cap = min(cap, facts.delivery_by - buffer)
                if facts.delivery_hours is not None:
                    cap = min(cap, latest_open_at_or_before(place["hours"], order["deadline"]) - 15)
                if cap > frm:
                    to = min(to, cap) if to is not None else cap
            if stop["kind"] == "pickup":
                first_pickup = False
            if to is not None and to > frm:
                stop = {**stop, "window": {"from": frm, "to": to}}
            elif "window" in stop:
                stop = {k: v for k, v in stop.items() if k != "window"}
            out.append(stop)
        return out

    def repost(self, gig_id: str) -> None:
        """A gig expired before a driver booked it: post its cars again. While the order can
        still be on time it goes out as new; else as a late car at steep fees."""
        action = self.posts.get(gig_id)
        if action is None or not any(
            self.leg_gigs.get(s["orderId"], {}).get(s["leg"]) == gig_id for s in action["stops"]
        ):
            return
        if self.is_combo(gig_id):
            self.repost_others(gig_id)
            return
        for stop in action["stops"]:
            self.leg_gigs.get(stop["orderId"], {}).pop(stop["leg"], None)
        order = self.orders.get(action["stops"][0]["orderId"])
        if order is None:
            return
        legs = [s["leg"] for s in action["stops"] if s["kind"] == "pickup"]
        retry = Retry(plates=any(s.get("withPlates") for s in action["stops"]),
                      manual_licence=action["audience"]["manualLicence"])
        self.post(order, legs, retry)

    def raise_fees(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if order is None:
            return
        self.urgent.add(order_id)
        self.urgent_at[order_id] = self.now
        self.send_post_of(order_id)
        for gig_id in self.gigs_of(order_id):
            if self.is_combo(gig_id) and gig_id not in self.booked:
                # The pair found no driver in time: split it, each car at its own fees.
                self.outbox.append({"type": "withdraw", "gigId": gig_id})
                self.dead_gigs.add(gig_id)
                self.repost_others(gig_id)
                return
        for gig_id in self.gigs_of(order_id):
            if gig_id not in self.booked:
                schedule = self.schedule_for(order, pickups(self.posts[gig_id]))
                self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": schedule})
                self.posts[gig_id] = {**self.posts[gig_id], "schedule": schedule}
        # Direct offers exist from level 3.
        if self.level >= 3:
            self._try_direct_offers_for_order(order_id)

    def retry(self, order_id: str, leg: int, reason: str) -> None:
        if order_id in self.orders:
            order = self.orders[order_id]
            self.leg_gigs.get(order_id, {}).pop(leg, None)
            car_not_there = reason in ("not_ready", "closed")
            pickup_from = retry_from(order, self.requests[order_id], leg, self.now) if car_not_there else 0
            self.post(order, [leg], Retry(pickup_from, manual_licence=reason == "licence", plates=reason == "no_plates"))

    # ── Feature 2: Direct offers ──

    def _try_direct_offers_for_order(self, order_id: str) -> None:
        """Send a direct offer for unbooked gigs of this order."""
        order = self.orders.get(order_id)
        if order is None:
            return
        for gig_id in self.gigs_of(order_id):
            if gig_id in self.booked or self.is_combo(gig_id):
                continue
            action = self.posts.get(gig_id)
            if action is None:
                continue
            pickup_place = next(
                (s["orderId"] for s in action["stops"] if s["kind"] == "pickup"), None
            )
            if pickup_place is None:
                continue
            # Find the actual place id from the order's leg
            for s in action["stops"]:
                if s["kind"] == "pickup":
                    leg_data = order["legs"][s["leg"]]
                    place_id = leg_data["pickup"]["placeId"]
                    need_exp = action["audience"]["experienced"]
                    need_manual = action["audience"]["manualLicence"]
                    driver_id = self.driver_mgr.best_driver_for_offer(
                        place_id, self.router, self.now,
                        need_experienced=need_exp,
                        need_manual=need_manual,
                        busy_drivers=self.busy_drivers,
                    )
                    if driver_id:
                        fee = self._offer_fee(order, action)
                        self.outbox.append({"type": "offer", "gigId": gig_id, "driverId": driver_id, "feeCents": fee})
                        self.driver_mgr.record_offer(driver_id, self.now)
                    break

    def _try_next_offer(self, gig_id: str) -> None:
        """After an offer is declined/expired, try the next best driver."""
        action = self.posts.get(gig_id)
        if action is None or gig_id in self.booked or self.is_combo(gig_id):
            return
        order_id = action["stops"][0]["orderId"]
        order = self.orders.get(order_id)
        if order is None or order_id not in self.urgent:
            return
        for s in action["stops"]:
            if s["kind"] == "pickup":
                place_id = order["legs"][s["leg"]]["pickup"]["placeId"]
                driver_id = self.driver_mgr.best_driver_for_offer(
                    place_id, self.router, self.now,
                    need_experienced=action["audience"]["experienced"],
                    need_manual=action["audience"]["manualLicence"],
                    busy_drivers=self.busy_drivers,
                )
                if driver_id:
                    fee = self._offer_fee(order, action)
                    self.outbox.append({"type": "offer", "gigId": gig_id, "driverId": driver_id, "feeCents": fee})
                    self.driver_mgr.record_offer(driver_id, self.now)
                break

    def _offer_fee(self, order: Order, action: PostAction) -> int:
        """Fee for a direct offer — generous to ensure acceptance."""
        base = self.requests[order["id"]]["referenceFeeCents"] * pickups(action) / len(order["legs"])
        multiplier = 1.8 if order["id"] in self.urgent else 1.4
        return max(1, int(base * multiplier + 0.5))

    # ── Fee schedules ──

    def fee_base(self, order: Order, cars: int) -> float:
        return self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])

    def fee_at(self, order: Order, cars: int, t: int) -> int:
        """The fee we want in the app at minute t: from the typical fee at posting, up to
        FEE_PLATEAU times it by the deadline warning (3 hours before the last pickup), then steep."""
        base = self.fee_base(order, cars)
        if order["id"] in self.urgent:
            since = max(0, t - self.urgent_at.get(order["id"], t))
            return max(1, int(base * min(2.5, 1.6 + since / 30 * 0.25) + 0.5))
        start = self.first_posted.get(order["id"], t)
        warn = self.eff_last_pickup(order) - 3 * HOUR
        span = awake_minutes(start, warn)
        hold = span * FEE_HOLD_SHARE
        if LOWBALL_OPEN < FEE_OPEN and span >= LOWBALL_MIN_SPAN and hold > 0:
            elapsed = awake_minutes(start, t)
            if elapsed < hold:  # ask less first: drivers for whom the car is cheap take it
                return max(1, int(base * (LOWBALL_OPEN + (FEE_OPEN - LOWBALL_OPEN) * elapsed / hold) + 0.5))
        frac = 1.0 if span <= 0 else min(1.0, max(0.0, awake_minutes(start, t) - hold) / max(1.0, span - hold))
        return max(1, int(base * (FEE_OPEN + (FEE_PLATEAU - FEE_OPEN) * frac) + 0.5))

    def schedule_for(self, order: Order, cars: int) -> list[FeeStep]:
        self.first_posted.setdefault(order["id"], self.now)
        if self.level == 1:
            return [{"at": self.now, "feeCents": self.fee_at(order, cars, self.now)}]
        if order["id"] in self.urgent:
            end = self.urgent_at.get(order["id"], self.now) + 4 * 30
        else:
            end = max(self.now, self.eff_last_pickup(order) - 3 * HOUR)
        step = max(REPRICE_EVERY, (end - self.now) // 40 + 1)
        steps: list[FeeStep] = []
        t = self.now
        while True:
            fee = self.fee_at(order, cars, t)
            if not steps or fee > steps[-1]["feeCents"]:
                steps.append({"at": t, "feeCents": fee})
            if t >= end or len(steps) >= 48:
                break
            t = min(end, t + step)
        return steps

    def reprice_level1(self) -> None:
        """Level 1 allows one fee step: raise open gigs to the fee they should have now."""
        for gig_id, action in list(self.posts.items()):
            if gig_id in self.booked or gig_id in self.dead_gigs:
                continue
            order = self.orders.get(action["stops"][0]["orderId"])
            if order is None or not any(self.leg_gigs.get(order["id"], {}).get(s["leg"]) == gig_id for s in action["stops"]):
                continue
            fee = self.fee_at(order, pickups(action), self.now)
            if fee > action["schedule"][-1]["feeCents"]:
                schedule = [{"at": self.now, "feeCents": fee}]
                self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": schedule})
                self.posts[gig_id] = {**action, "schedule": schedule}

    def next_wake(self) -> int:
        """The next minute we want to act without an event: our own deadline warnings, level 1 fee
        raises, and a look for nearby drivers to offer open gigs to."""
        wake = self.now + DAY
        open_any = False
        for oid, order in self.orders.items():
            gigs = self.gigs_of(oid)
            if not gigs or any(g in self.booked for g in gigs):
                continue
            open_any = True
            if oid not in self.urgent:
                t = self.eff_last_pickup(order) - 3 * HOUR
                if t > self.now:
                    wake = min(wake, t)
        if open_any:
            wake = min(wake, self.now + (REPRICE_EVERY if self.level == 1 else HOUR))
        return max(self.now + 1, wake)

    # ── Late drivers ──

    def driver_late(self, gig_id: str, pickup_at: int) -> None:
        """A booked driver will collect the first car late. Keep them when the car still arrives on time,
        or when no other driver could do better; else release them, free, and post the gig again."""
        action = self.posts.get(gig_id)
        if action is None or self.is_combo(gig_id):
            return
        order = self.orders.get(action["stops"][0]["orderId"])
        if order is None:
            return
        legs = [s["leg"] for s in action["stops"] if s["kind"] == "pickup"]
        drive = sum(self.router.drive_minutes(order["legs"][l]["pickup"]["placeId"], order["legs"][l]["delivery"]["placeId"]) for l in legs)
        if pickup_at + drive <= order["deadline"]:
            return
        if order["lastPickupAt"] - self.now < 3 * HOUR:
            return
        self.outbox.append({"type": "release", "gigId": gig_id})
        self.booked.discard(gig_id)
        self._free(self.gig_driver.get(gig_id, ""), gig_id)
        self.dead_gigs.add(gig_id)
        for leg in legs:
            self.leg_gigs.get(order["id"], {}).pop(leg, None)
        self.post(order, legs)

    # ── Multi-car gigs ──

    def is_combo(self, gig_id: str) -> bool:
        action = self.posts.get(gig_id)
        return action is not None and len({s["orderId"] for s in action["stops"]}) > 1

    def repost_others(self, gig_id: str, skip: Optional[str] = None) -> None:
        """Post each order of a combo gig again on its own (all but `skip`)."""
        action = self.posts.get(gig_id)
        if action is None:
            return
        by_order: dict[str, list[int]] = {}
        for st in action["stops"]:
            if st["kind"] == "pickup":
                by_order.setdefault(st["orderId"], []).append(st["leg"])
        for oid, legs in by_order.items():
            if oid == skip:
                continue
            order = self.orders.get(oid)
            if order is None:
                continue
            legs = [l for l in legs if self.leg_gigs.get(oid, {}).get(l) == gig_id]
            for l in legs:
                self.leg_gigs[oid].pop(l, None)
            if legs:
                self.post(order, legs)

    def combo_candidate(self, order: Order) -> bool:
        """A single car between dealer sites, with time to spare."""
        if order["id"] in self.urgent or len(order["legs"]) != 1 or undelivered(order) != [0]:
            return False
        leg = order["legs"][0]
        if leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"]:
            return False
        return self.eff_last_pickup(order) - 3 * HOUR - self.now >= HOUR

    def chain_fits(self, x: Order, y: Order) -> bool:
        """Can one driver deliver x, then collect y nearby, keeping both orders on time with margin?"""
        lx = noted_legs(x["legs"], self.facts_of(self.requests[x["id"]]))[0]
        ly = noted_legs(y["legs"], self.facts_of(self.requests[y["id"]]))[0]
        gap = self.router.drive_minutes(lx["delivery"]["placeId"], ly["pickup"]["placeId"])
        if gap > COMBO_GAP:
            return False
        dx = self.router.drive_minutes(lx["pickup"]["placeId"], lx["delivery"]["placeId"])
        dy = self.router.drive_minutes(ly["pickup"]["placeId"], ly["delivery"]["placeId"])
        if dx + gap + dy > COMBO_MAX_DRIVE:
            return False
        bx = SAFETY_MIN + int(dx * SAFETY_SHARE)
        by = SAFETY_MIN + int(dy * SAFETY_SHARE)
        t = earliest_open_at_or_after(lx["pickup"]["hours"], max(self.now + HOUR, lx["pickup"]["window"]["from"]))
        if t > x["lastPickupAt"] - bx:
            return False
        arrive_x = t + dx
        if arrive_x > x["deadline"] - bx:
            return False
        ty = earliest_open_at_or_after(ly["pickup"]["hours"], max(arrive_x + gap, ly["pickup"]["window"]["from"]))
        if ty > y["lastPickupAt"] - by or ty + dy > y["deadline"] - by:
            return False
        return True

    def try_combo(self, order: Order) -> bool:
        """Chain a newly won order with one of our open single-car gigs. True when posted as a pair."""
        if not self.combo_candidate(order):
            return False
        best = None
        for oid, other in self.orders.items():
            if oid == order["id"] or not self.combo_candidate(other):
                continue
            gigs = self.gigs_of(oid)
            if len(gigs) != 1:
                continue
            gig = gigs[0]
            if gig in self.booked or gig in self.dead_gigs or self.is_combo(gig) or self.open_offers.get(gig, 0):
                continue
            a, b = other["legs"][0], order["legs"][0]
            for x, y, lx, ly in ((other, order, a, b), (order, other, b, a)):
                gap = self.router.drive_minutes(lx["delivery"]["placeId"], ly["pickup"]["placeId"])
                if gap <= COMBO_GAP and (best is None or gap < best[0]) and self.chain_fits(x, y):
                    best = (gap, x, y, gig, oid)
        if best is None:
            return False
        _, x, y, gig, oid = best
        self.outbox.append({"type": "withdraw", "gigId": gig})
        self.dead_gigs.add(gig)
        self.leg_gigs.pop(oid, None)
        self.post_combo(x, y)
        self.combo_count += 1
        return True

    def post_combo(self, x: Order, y: Order) -> None:
        stops: list[StopRef] = []
        audience: Audience = {"experienced": False, "manualLicence": False}
        for o in (x, y):
            facts = self.facts_of(self.requests[o["id"]])
            stops += self.tighten(o, stops_for(o, [0], None, Retry()), facts)
            audience["experienced"] = audience["experienced"] or o["legs"][0]["car"]["premium"]
            audience["manualLicence"] = audience["manualLicence"] or facts.manual
            self.first_posted.setdefault(o["id"], self.now)
        stops = without_windows(stops, self.now)
        end = max(self.now, min(self.eff_last_pickup(o) for o in (x, y)) - 3 * HOUR)
        step = max(REPRICE_EVERY, (end - self.now) // 40 + 1)
        schedule: list[FeeStep] = []
        t = self.now
        while True:
            fee = max(1, int(COMBO_FACTOR * (self.fee_at(x, 1, t) + self.fee_at(y, 1, t))))
            if not schedule or fee > schedule[-1]["feeCents"]:
                schedule.append({"at": t, "feeCents": fee})
            if t >= end or len(schedule) >= 48:
                break
            t = min(end, t + step)
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": schedule})

    # ── Gig tracking ──

    def _unbooked_count(self) -> int:
        count = 0
        for oid in self.orders:
            gids = self.gigs_of(oid)
            if not gids or not any(g in self.booked for g in gids):
                count += 1
        return count

    def gigs_of(self, order_id: str) -> list[str]:
        return list(dict.fromkeys(self.leg_gigs.get(order_id, {}).values()))

    def carry(self, action: PostAction, gig_id: str) -> None:
        for stop in action["stops"]:
            self.leg_gigs.setdefault(stop["orderId"], {})[stop["leg"]] = gig_id
        self.posts[gig_id] = action
        self.gig_posted_at[gig_id] = self.now

    def withdraw(self, order_id: str) -> bool:
        self.send_post_of(order_id)
        gigs = self.gigs_of(order_id)
        if any(gig_id in self.booked for gig_id in gigs):
            return False
        self.leg_gigs.pop(order_id, None)
        self.outbox += [{"type": "withdraw", "gigId": gig_id} for gig_id in gigs]
        for gig_id in gigs:
            if self.is_combo(gig_id):
                self.dead_gigs.add(gig_id)
                self.repost_others(gig_id, skip=order_id)
        return True

    def send_post_of(self, order_id: str) -> None:
        if any(a["type"] == "post" and any(s["orderId"] == order_id for s in a["stops"]) for a in self.outbox):
            self.send()

    def send(self) -> None:
        actions, self.outbox = self.outbox, []
        if actions:
            self.read_results(actions, self.world.act(actions))

    def read_results(self, actions: list[Action], results: list[ActionResult]) -> None:
        retry_orders: list[str] = []
        for action, result in zip(actions, results):
            if action["type"] == "post" and result["ok"] is True:
                self.carry(action, result["id"])
            elif action["type"] == "withdraw" and result["ok"] is not True:
                # A driver took it first: the gig still carries its cars.
                gig = action["gigId"]
                if gig in self.dead_gigs and gig in self.posts:
                    self.dead_gigs.discard(gig)
                    for st in self.posts[gig]["stops"]:
                        if st["orderId"] in self.orders and self.orders[st["orderId"]]["deliveredAt"][st["leg"]] is None:
                            self.leg_gigs.setdefault(st["orderId"], {})[st["leg"]] = gig
            elif action["type"] == "post" and result["ok"] is not True and len({s["orderId"] for s in action["stops"]}) > 1:
                retry_orders += [s["orderId"] for s in action["stops"] if s["kind"] == "pickup"]
        # A pair the world refused: post each car that has no live gig on its own.
        for oid in dict.fromkeys(retry_orders):
            order = self.orders.get(oid)
            if order is not None and not self.gigs_of(oid) and undelivered(order):
                self.post(order, undelivered(order))

    # ── Periodic maintenance (called each advance step) ──

    def maintain(self) -> None:
        """Run periodic checks: our own deadline warnings, level 1 fee raises, direct offers."""
        for oid, order in list(self.orders.items()):
            if oid in self.urgent or not undelivered(order):
                continue
            gigs = self.gigs_of(oid)
            if gigs and not any(g in self.booked for g in gigs) and self.now >= self.eff_last_pickup(order) - 3 * HOUR:
                self.raise_fees(oid)
        if self.level == 1:
            self.reprice_level1()
        if self.level >= 3:
            self.offer_nearby()
            self._offer_stuck_gigs()

    def offer_nearby(self) -> None:
        """Offer open gigs to our own drivers who just finished near the pickup: for them the car is
        cheap, and a yes books the gig at once."""
        today = self.now // DAY
        if not (7 * HOUR <= self.now % DAY < 21 * HOUR):
            return
        free = {d: v for d, v in self.last_seen.items()
                if d not in self.busy_drivers and self.now - v[1] <= OFFER_RECENT and v[1] // DAY == today}
        if not free:
            return
        for oid, order in self.orders.items():
            for gig_id in self.gigs_of(oid):
                if gig_id in self.booked or gig_id in self.dead_gigs or self.open_offers.get(gig_id, 0) >= 2 \
                        or self.is_combo(gig_id):
                    continue
                action = self.posts.get(gig_id)
                if action is None:
                    continue
                first = next(s for s in action["stops"] if s["kind"] == "pickup")
                place = order["legs"][first["leg"]]["pickup"]["placeId"]
                tried = self.offered.setdefault(gig_id, set())
                cands = []
                for d, (at_place, _) in free.items():
                    if d in tried or not self.driver_mgr.can_offer(d, self.now):
                        continue
                    if action["audience"]["experienced"] and not self.driver_mgr.is_experienced(d):
                        continue
                    prof = self.driver_mgr.drivers.get(d)
                    if action["audience"]["manualLicence"] and (prof is None or prof["licence"] != "manual"):
                        continue
                    dist = self.router.drive_minutes(at_place, place)
                    if dist <= OFFER_NEAR_MIN:
                        cands.append((dist, d))
                cands.sort()
                for _, d in cands[: 2 - self.open_offers.get(gig_id, 0)]:
                    fee = self.fee_at(order, pickups(action), self.now)
                    self.outbox.append({"type": "offer", "gigId": gig_id, "driverId": d, "feeCents": fee})
                    self.driver_mgr.record_offer(d, self.now)
                    tried.add(d)
                    self.open_offers[gig_id] = self.open_offers.get(gig_id, 0) + 1

    def _offer_stuck_gigs(self) -> None:
        """Send direct offers for gigs that have been open too long without a booking."""
        if self.level < 2:
            return
        threshold = HOUR if self.level >= 8 else (2 * HOUR if self.level >= 7 else 3 * HOUR)
        for order_id, order in self.orders.items():
            if order_id in self.released:
                continue
            for gig_id in self.gigs_of(order_id):
                if gig_id in self.booked:
                    continue
                posted_at = self.gig_posted_at.get(gig_id, self.now)
                last = self.last_offer_at.get(order_id, -DAY)
                if self.now - posted_at >= threshold and self.now - last >= HOUR:
                    self.last_offer_at[order_id] = self.now
                    self._try_direct_offers_for_order(order_id)
                    break

    # ── Rejoin after restart ──

    def rejoin(self, snapshot: DeskSnapshot, history: list[DeskEvent]) -> None:
        posted: dict[str, list[Gig]] = {}
        self.booked = {gig["id"] for gig in snapshot["gigs"] if gig["status"] in ("booked", "working", "done")}
        for gig in snapshot["gigs"]:
            posted.setdefault(gig["stops"][0]["orderId"], []).append(gig)
            if gig["status"] in ("open", "booked", "working"):
                self.carry({"type": "post", "stops": gig["stops"], "audience": gig["audience"], "schedule": gig["schedule"]}, gig["id"])
                self.gig_posted_at[gig["id"]] = gig["postedAt"]
            if gig["driverId"]:
                self.busy_drivers.add(gig["driverId"])
        self.requests = {e["request"]["id"]: e["request"] for e in history if e["kind"] == "price_request"}
        self.urgent = {e["orderId"] for e in history if e["kind"] == "deadline_close"}
        self.orders = {o["id"]: o for o in snapshot["orders"] if o["id"] in self.requests and o["status"] != "cancelled"}
        for order in self.orders.values():
            if order["status"] == "waiting" and not self.gigs_of(order["id"]):
                self.post(order, undelivered(order))
        for event in history:
            order = self.orders.get(event.get("orderId", ""))
            if order is None:
                continue
            if event["kind"] == "visit_failed" and order["deliveredAt"][event["leg"]] is None:
                later = [g for g in posted.get(order["id"], []) if g["postedAt"] >= event["at"] and g["id"] != event["gigId"]]
                if not any(stop["leg"] == event["leg"] for gig in later for stop in gig["stops"]):
                    self.retry(order["id"], event["leg"], event["reason"])
        for request in snapshot["openRequests"]:
            self.quote(request)


# Test runs can override the constants above: SPREAD_TUNE='{"LEAD_MIN": {"8": 240}, "STUCK_STOP": 4}'
for _k, _v in json.loads(os.environ.get("SPREAD_TUNE") or "{}").items():
    if _k in globals():
        if isinstance(globals()[_k], dict) and isinstance(_v, dict):
            globals()[_k].update({int(a): b for a, b in _v.items()})
        else:
            globals()[_k] = _v


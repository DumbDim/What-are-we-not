"""Dynamic pricing engine. Decides what to quote per request based on supply, demand, and trust."""
from __future__ import annotations
import json
import os
from typing import Optional
from spread.types import PriceRequest, DeliveryOption
from .winmodel import WinModel

HOUR = 60
DAY1_DISCOUNT, DISCOUNT_UNTIL = 1.0, 1440  # level 8: multiply quotes by this before DISCOUNT_UNTIL (Tuesday 00:00) to build trust early
FLOOR_QUOTE = True  # levels 7-8: quote the least profitable price instead of declining a thin-margin request
HOT_PICKUPS, HOT_PRICE = ["vantaa", "hyvinkaa"], 0.93  # level 8: price factor for pickups in our busiest towns
LOW_DESTS, LOW_DEST_MARKUP = ["kajaani", "kemi", "kotka", "savonlinna"], 1.25  # level 8: extra markup to places that earn little
MODEL_PRICING, MODEL_WARMUP, MODEL_RMAX = False, 60, 2.0
MODEL_LATE = False  # (a) subtract expected lateness cost and a capacity charge that grow with area load
LATE_BASE, LATE_LOAD, CAP_LAMBDA = 0.02, 0.30, 0.5  # P(late) = base + k*load^2; capacity charge = lambda*load*fee
MODEL_LANE_FEE = False  # (b) expected fee from bookings we have seen per pickup place
MODEL_TRUST_REGION = 0.0  # (c) keep the model within +-this share of the rule price (0 = no limit)
MODEL_OPTIONS = False  # (d) steer dealers to slower options: within_3_days cheaper, same_day dearer when busy  # levels 7-8: price by the learned win model after this many results
NORTH_DESTS, NORTH_PRICE = ["oulu", "jyvaskyla", "rovaniemi", "kokkola", "ylivieska"], 1.0  # level 8: price factor for long lanes from the south into these places
OPTION_ADJ = {"same_day": 0.98, "next_day": 1.0, "within_3_days": 1.02}  # price factor by delivery option
LANE_PRIOR = False  # level 8: start each lane at the competitor price learned from earlier worlds (lane_prior.json)
BULK_ON, BULK_MIN, BULK_MARKUP = False, 6, 1.15  # level 8: a batch of this many requests is a bulk order: quote it dearer
DEALER_SHARE_ON, DEALER_SHARE_MAX, DEALER_SHARE_MARKUP = False, 0.6, 1.05  # level 8: winning too much of a dealer: price up
WAR_PRICE = 0.82  # level 8: on a detected price-war day, multiply quotes by this (1.0 = sit it out)
MONDAY_MARKUP = None  # level 8: starting markup on Monday instead of L8_MARKUP (None = same)
L7_MARKUP, L8_MARKUP = 1.30, 1.40  # starting price over the list price; learned lane prices adjust it
EXPECTED_FEE = 1.2  # times the typical fee
MIN_MARGIN = 0.10  # least share above the expected fee
# Learned prices: each pickup place and delivery place has a factor that rises after a win and falls
# after a loss. With these steps it settles where we win about 45% of our quotes.
LANE_UP, LANE_DOWN, LANE_MIN, LANE_MAX = 0.020, 0.016, 0.85, 1.25
WAR_WINDOW, WAR_DROP, WAR_FLOOR, WAR_WARMUP = 20, 1 / 3, 0.10, 40  # one price war a week: last 20 quotes win under max(10%, a third of usual), after 40 results  # price war: our recent win rate everywhere fell under a third of our usual rate
OUTAGE_STREAK, OUTAGE_BONUS = 5, 1.15  # outage: our last quotes from one pickup place all won


class Pricer:
    """Tracks competitor behavior and dealer trust to set optimal prices."""

    def __init__(self, level: int):
        self.level = level
        self.dealer_wins: dict[str, int] = {}
        self.dealer_total: dict[str, int] = {}
        self.won_count = 0
        self.late_count = 0
        self.f_pickup: dict[str, float] = {}
        self.f_delivery: dict[str, float] = {}
        self.quoted: dict[str, tuple[str, str]] = {}  # request id -> (pickup, delivery) of a real quote
        self.recent: list[bool] = []  # outcomes of our last real quotes, everywhere
        self.by_pickup: dict[str, list[bool]] = {}
        self.war_day = -1  # the day we saw a price war
        self.usual_win = None  # long-run share of our quotes that win
        self.model = WinModel()
        self.model_x: dict[str, tuple[dict, float]] = {}  # request id -> (features, price/list) of a real quote
        self.model_quotes = 0  # quotes priced by the model
        self.bulk_factor = 1.0  # set by the desk for requests of a bulk order
        self.dealer_hist: dict[str, list[bool]] = {}  # dealer -> outcomes of our last real quotes
        self.quoted_dealer: dict[str, str] = {}
        self.lane_prior: dict[str, float] = {}
        if LANE_PRIOR:
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lane_prior.json")) as fh:
                    self.lane_prior = json.load(fh)["lanes"]
            except (OSError, ValueError, KeyError):
                self.lane_prior = {}
        self.load_fn = None  # set by the desk: pickup place -> our waiting orders there / the area's limit
        self.fee_ratio: dict[str, float] = {}  # pickup place -> booked fee / typical fee, running mean
        self.results_seen = 0
        self.war_seen = False  # the rules give one price war a week
        self.outage: dict[str, int] = {}  # pickup place -> day of its outage
        self.now = 0

    def on_time_rate(self) -> float:
        delivered = self.won_count - self.late_count
        return delivered / max(1, self.won_count)

    def quote_prices(self, request: PriceRequest, driver_supply: float = 1.0,
                     active_orders: int = 0, unbooked: int = 0) -> dict[str, int]:
        prices = {}
        for option in request["options"]:
            price = self._price_option(request, option, driver_supply, active_orders, unbooked)
            if price is not None:
                prices[option["kind"]] = price
        if MODEL_PRICING and self.level >= 7 and prices and self.model.seen >= MODEL_WARMUP:
            prices = self._model_prices(request, prices)
        return prices

    def _model_prices(self, request: PriceRequest, rule_prices: dict[str, int]) -> dict[str, int]:
        """Re-price the options we would quote at the ratio with the highest expected margin."""
        ref = request["referenceFeeCents"]
        first = next(o for o in request["options"] if o["kind"] in rule_prices)
        pickup = request["legs"][0]["pickup"]["placeId"]
        fee = ref * (self.fee_ratio.get(pickup, EXPECTED_FEE) if MODEL_LANE_FEE else EXPECTED_FEE)
        floor = ref * EXPECTED_FEE * (1 + MIN_MARGIN)
        r_min, r_max = max(0.9, floor / first["listPriceCents"]), MODEL_RMAX
        if MODEL_TRUST_REGION:
            r_rule = rule_prices[first["kind"]] / first["listPriceCents"]
            r_min = max(r_min, r_rule * (1 - MODEL_TRUST_REGION))
            r_max = min(r_max, r_rule * (1 + MODEL_TRUST_REGION))
            r_max = max(r_max, r_min)
        if r_min > MODEL_RMAX:
            return {}
        trust = self.dealer_wins.get(request["dealerId"], 0)
        x = WinModel.features(request, self.now, trust)
        load = min(1.5, max(0.0, self.load_fn(pickup))) if (self.load_fn and (MODEL_LATE or MODEL_OPTIONS)) else 0.0
        if MODEL_LATE:
            p_late = min(0.9, LATE_BASE + LATE_LOAD * load * load)
            L = first["listPriceCents"]

            def value(rr: float) -> float:
                price = rr * L
                # a late order refunds part of the price and halves this dealer's trust count
                late_cost = p_late * (0.5 * price + 0.025 * trust * price)
                return self.model.p_win(x, rr) * ((price - fee) - late_cost - CAP_LAMBDA * load * fee)

            r, best, rr = r_min, -1e18, r_min
            while rr <= r_max + 1e-9:
                v = value(rr)
                if v > best:
                    r, best = rr, v
                rr += 0.02
            if best <= 0:
                return {}  # no price is worth the driver it takes here now
        else:
            r = self.model.best_ratio(x, first["listPriceCents"], fee, r_min, r_max)
        if self.level >= 8 and request["legs"][0]["delivery"]["placeId"] in LOW_DESTS:
            r *= LOW_DEST_MARKUP
        if self.level >= 8 and WAR_PRICE != 1.0 and self.war_day == self.now // 1440 and 8 * 60 <= self.now % 1440 < 18 * 60:
            r = max(r_min, r * WAR_PRICE)
        out = {}
        for o in request["options"]:
            if o["kind"] not in rule_prices:
                continue
            adj = OPTION_ADJ.get(o["kind"], 1.0)
            if MODEL_OPTIONS:
                adj = (1.03 if load > 0.5 else 1.0) if o["kind"] == "same_day" else 0.95 if o["kind"] == "within_3_days" else 1.0
            out[o["kind"]] = max(int(o["listPriceCents"] * r * adj), int(ref * 1.15), int(floor))
        self.model_quotes += 1
        return out

    def _price_option(self, request: PriceRequest, option: DeliveryOption,
                      driver_supply: float, active_orders: int, unbooked: int) -> Optional[int]:
        list_price = option["listPriceCents"]
        ref_fee = request["referenceFeeCents"]

        if self.level <= 3:
            return list_price

        base = list_price

        if self.level >= 7:
            markup = L7_MARKUP if self.level == 7 else L8_MARKUP
            if self.level >= 8 and MONDAY_MARKUP is not None and self.now < 1440:
                markup = MONDAY_MARKUP
            base = int(list_price * markup)
        elif self.level >= 4:
            base = int(list_price * 1.05)

        dealer_id = request["dealerId"]
        trust_count = self.dealer_wins.get(dealer_id, 0)
        if self.level >= 8 and trust_count > 0:
            # Each on-time order raises what this dealer accepts from us by 1%, up to 25%.
            trust_bonus = min(trust_count, 25) * 0.01
            base = int(base * (1 + trust_bonus))

        floor = int(ref_fee * EXPECTED_FEE * (1 + MIN_MARGIN))
        if self.level >= 7:
            # Judge the margin on the price we quote against the fee we expect to pay
            # (drivers mostly book between 1.0 and 1.6 times the typical fee).
            if base < floor and not FLOOR_QUOTE:
                return None
        elif driver_supply < 0.5:
            if ref_fee > list_price * 0.7:
                return None
            base = int(base * 1.10)

        if self.level >= 4:
            base = int(base * self.lane_factor(request))
        if self.level >= 8:
            lane = request["legs"][0]["pickup"]["placeId"] + "|" + request["legs"][0]["delivery"]["placeId"]
            if LANE_PRIOR:
                base = int(base * self.lane_prior.get(lane, 1.0))
            if BULK_ON and self.bulk_factor != 1.0:
                base = int(base * self.bulk_factor)
            if DEALER_SHARE_ON:
                hist = self.dealer_hist.get(request["dealerId"], [])
                if len(hist) >= 5 and sum(hist) / len(hist) >= DEALER_SHARE_MAX:
                    base = int(base * DEALER_SHARE_MARKUP)
            if request["legs"][0]["pickup"]["placeId"] in HOT_PICKUPS:
                base = int(base * HOT_PRICE)
            if request["legs"][0]["delivery"]["placeId"] in LOW_DESTS:
                base = int(base * LOW_DEST_MARKUP)
            if request["legs"][0]["delivery"]["placeId"] in NORTH_DESTS and request["legs"][0]["pickup"]["placeId"] not in NORTH_DESTS:
                base = int(base * NORTH_PRICE)  # our most profitable lanes per car: win more of them

        base = int(base * OPTION_ADJ.get(option["kind"], 1.0))

        if self.level >= 8 and WAR_PRICE != 1.0 and self.war_day == self.now // 1440                 and 8 * 60 <= self.now % 1440 < 18 * 60:
            base = int(base * WAR_PRICE)  # compete while the competitor's discount runs: 08:00-18:00

        if self.level >= 8 and DAY1_DISCOUNT != 1.0 and self.now < DISCOUNT_UNTIL:
            base = int(base * DAY1_DISCOUNT)

        min_price = int(ref_fee * 1.15)
        if self.level >= 7 and FLOOR_QUOTE:
            min_price = max(min_price, floor)  # never below the least profitable price
        return max(base, min_price)

    # ── Learned prices ──

    def lane_factor(self, request: PriceRequest) -> float:
        pickup = request["legs"][0]["pickup"]["placeId"]
        delivery = request["legs"][0]["delivery"]["placeId"]
        f = self.f_pickup.get(pickup, 1.0) * self.f_delivery.get(delivery, 1.0)
        if self.outage.get(pickup) == self.now // 1440:
            f *= OUTAGE_BONUS
        return max(LANE_MIN, min(LANE_MAX, f))

    def record_quote(self, request: PriceRequest, prices: Optional[dict] = None) -> None:
        self.quoted_dealer[request["id"]] = request["dealerId"]
        self.quoted[request["id"]] = (request["legs"][0]["pickup"]["placeId"], request["legs"][0]["delivery"]["placeId"])
        if MODEL_PRICING and prices:
            first = next((o for o in request["options"] if o["kind"] in prices), None)
            if first is not None:
                x = WinModel.features(request, self.now, self.dealer_wins.get(request["dealerId"], 0))
                self.model_x[request["id"]] = (x, prices[first["kind"]] / first["listPriceCents"])

    def record_result(self, request_id: str, won: bool, now: int) -> None:
        """Learn from a dealer's answer to one of our real quotes."""
        self.now = now
        dealer = self.quoted_dealer.pop(request_id, None)
        if dealer is not None:
            h = self.dealer_hist.setdefault(dealer, [])
            h.append(won)
            del h[:-10]
        mx = self.model_x.pop(request_id, None)
        if mx is not None and not (self.war_day == now // 1440 and 8 * 60 <= now % 1440 < 18 * 60):
            self.model.update(mx[0], mx[1], won)  # price-war hours are unusual: do not learn from them
        lane = self.quoted.pop(request_id, None)
        if lane is None or self.level < 4:
            return
        pickup, delivery = lane
        if DAY1_DISCOUNT != 1.0 and now < DISCOUNT_UNTIL:
            return  # discounted wins say nothing about the normal price
        day = now // 1440
        self.recent = (self.recent + [won])[-WAR_WINDOW:]
        hist = self.by_pickup.setdefault(pickup, [])
        hist.append(won)
        del hist[:-OUTAGE_STREAK]
        self.results_seen += 1
        rate = self.recent.count(True) / len(self.recent)
        if not self.war_seen and self.usual_win is not None and self.results_seen >= WAR_WARMUP                 and len(self.recent) >= WAR_WINDOW and rate <= max(WAR_FLOOR, self.usual_win * WAR_DROP)                 and 8 * 60 <= now % 1440 < 18 * 60:
            self.war_day = day  # the competitor cut its prices for the day
            self.war_seen = True
        if self.war_day != day:
            self.usual_win = float(won) if self.usual_win is None else 0.98 * self.usual_win + 0.02 * won
        if len(hist) >= OUTAGE_STREAK and all(hist):
            self.outage[pickup] = day  # the competitor seems down here today
        if won:
            step = 1 + LANE_UP
        elif self.war_day == day:
            return
        else:
            step = 1 - LANE_DOWN
        for table, place in ((self.f_pickup, pickup), (self.f_delivery, delivery)):
            table[place] = max(LANE_MIN, min(LANE_MAX, table.get(place, 1.0) * step))

    def record_fee(self, pickup: str, ratio: float) -> None:
        """A driver booked one of our gigs at `ratio` times its typical fee."""
        old = self.fee_ratio.get(pickup, EXPECTED_FEE)
        self.fee_ratio[pickup] = 0.85 * old + 0.15 * ratio

    def record_win(self, dealer_id: str, price: int, option: str) -> None:
        self.dealer_total[dealer_id] = self.dealer_total.get(dealer_id, 0) + 1
        self.won_count += 1

    def record_delivery(self, dealer_id: str, on_time: bool) -> None:
        if on_time:
            self.dealer_wins[dealer_id] = self.dealer_wins.get(dealer_id, 0) + 1
        else:
            self.late_count += 1
            current = self.dealer_wins.get(dealer_id, 0)
            self.dealer_wins[dealer_id] = current // 2

    def record_loss(self, request_id: str) -> None:
        pass


# Test runs can override the constants above: SPREAD_TUNE='{"LEAD_MIN": {"8": 240}, "STUCK_STOP": 4}'
for _k, _v in json.loads(os.environ.get("SPREAD_TUNE") or "{}").items():
    if _k in globals():
        if isinstance(globals()[_k], dict) and isinstance(_v, dict):
            globals()[_k].update({(int(a) if str(a).isdigit() else a): b for a, b in _v.items()})
        else:
            globals()[_k] = _v


"""Win-probability pricing: learn P(win | price, lane, time, trust) from our own quote results and
quote the price that maximises expected margin, P(win) * (price - expected driver fee).

A logistic regression, updated online with one gradient step per quote result. The price enters as
r = our price / list price; its weight is kept negative, since a dearer quote never wins more often.
"""
from __future__ import annotations
import math

PLACES_DIM = 64  # hashed slots for pickup and delivery places


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class WinModel:
    def __init__(self, lr: float = 0.05, l2: float = 0.002, prior_bias: float = 8.6, prior_slope: float = -6.0):
        # Prior: about 40% win at 1.5x list, falling fast above that (RULES: above the competitor it falls quickly).
        self.w: dict[str, float] = {"bias": prior_bias, "r": prior_slope}
        self.lr, self.l2 = lr, l2
        self.seen = 0

    @staticmethod
    def features(request, now: int, trust: int) -> dict[str, float]:
        leg = request["legs"][0]
        hour = now % 1440 // 60
        return {
            "bias": 1.0,
            "p:" + leg["pickup"]["placeId"]: 1.0,
            "d:" + leg["delivery"]["placeId"]: 1.0,
            "h:" + ("am" if hour < 12 else "mid" if hour < 15 else "pm"): 1.0,
            "day:" + str(now // 1440 % 7): 1.0,
            "trust": min(trust, 25) / 25.0,
            "opts": len(request["options"]) / 3.0,
        }

    def z(self, x: dict[str, float], r: float) -> float:
        return sum(self.w.get(k, 0.0) * v for k, v in x.items()) + self.w["r"] * r

    def p_win(self, x: dict[str, float], r: float) -> float:
        return _sigmoid(self.z(x, r))

    def update(self, x: dict[str, float], r: float, won: bool) -> None:
        g = (1.0 if won else 0.0) - self.p_win(x, r)
        for k, v in x.items():
            reg = 0.0 if k == "bias" else self.l2 * self.w.get(k, 0.0)
            self.w[k] = self.w.get(k, 0.0) + self.lr * (g * v - reg)
        self.w["r"] = min(-1.0, self.w["r"] + self.lr * g * r)  # dearer never wins more
        self.seen += 1

    def best_ratio(self, x: dict[str, float], list_price: int, fee: float, r_min: float, r_max: float,
                   step: float = 0.02) -> float:
        """The price/list ratio in [r_min, r_max] with the highest expected margin."""
        best_r, best_v = r_min, -1e18
        r = r_min
        while r <= r_max + 1e-9:
            v = self.p_win(x, r) * (r * list_price - fee)
            if v > best_v:
                best_r, best_v = r, v
            r += step
        return best_r

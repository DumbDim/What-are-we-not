"""Offline stats over dumped event logs: quote win rates, lead times, booking fee ratios.

Usage: python tools/stats.py tools/runs/L8_*.json
"""
from __future__ import annotations
import collections
import glob
import json
import sys

DAY = 1440


def awake(a, b):
    """Minutes in [07:00, 22:00) between a and b."""
    tot = 0
    t = a
    while t < b:
        d = t // DAY * DAY
        lo, hi = d + 420, d + 1320
        s, e = max(t, lo), min(b, hi)
        if e > s:
            tot += e - s
        t = d + DAY
    return tot


def main(files):
    lead_bins = collections.defaultdict(lambda: [0, 0])
    ratio = []
    quoted = won = 0
    for f in files:
        ev = json.load(open(f))
        reqs = {e["request"]["id"]: e["request"] for e in ev if e["kind"] == "price_request"}
        won_o = {e["order"]["id"]: e for e in ev if e["kind"] == "order_won"}
        lost = {e["requestId"] for e in ev if e["kind"] == "order_lost"}
        quoted += len(won_o) + len(lost)
        won += len(won_o)
        deliv = collections.defaultdict(list)
        cancelled = {e["orderId"] for e in ev if e["kind"] == "order_cancelled"}
        for e in ev:
            if e["kind"] == "delivered":
                deliv[e["orderId"]].append(e["at"])
        gig_order = {}
        for e in ev:
            if e["kind"] == "ledger" and e["line"]["gigId"] and e["line"]["orderId"]:
                gig_order[e["line"]["gigId"]] = e["line"]["orderId"]
            if e["kind"] in ("picked_up", "delivered"):
                gig_order[e["gigId"]] = e["orderId"]
        for e in ev:
            if e["kind"] == "gig_booked" and e["gigId"] in gig_order:
                oid = gig_order[e["gigId"]]
                r = reqs.get(oid)
                if r:
                    ratio.append(e["feeCents"] / r["referenceFeeCents"] * len(r["legs"]))
        for oid, w in won_o.items():
            if oid in cancelled:
                continue
            o = w["order"]
            lead = awake(w["at"], o["lastPickupAt"])
            b = min(lead // 60, 12)
            ok = len(deliv[oid]) >= len(o["legs"]) and max(deliv[oid]) <= o["deadline"]
            lead_bins[b][0] += 1
            lead_bins[b][1] += ok
    print(f"quotes decided {quoted}, won {won} ({won / max(1, quoted):.0%})")
    print("awake lead hours -> orders, on-time share")
    for b in sorted(lead_bins):
        n, k = lead_bins[b]
        print(f"  {b:>2}h  {n:>4}  {k / n:6.1%}")
    ratio.sort()
    if ratio:
        q = lambda p: ratio[int(p * (len(ratio) - 1))]
        print(f"booked fee / ref (per car): n={len(ratio)} p10={q(.1):.2f} p50={q(.5):.2f} p90={q(.9):.2f}")


if __name__ == "__main__":
    files = [g for a in sys.argv[1:] for g in glob.glob(a)]
    main(files)

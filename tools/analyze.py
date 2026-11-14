"""Play practice worlds with the dispatcher and explain every late order.

Usage: python tools/analyze.py --level 1 --seeds a b c [--quiet] [--keep]
"""
from __future__ import annotations
import argparse
import collections
import concurrent.futures as cf
import io
import json
import os
import sys
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import run  # noqa: E402  (loads .env)
from spread.client import Client  # noqa: E402
from dispatcher.routing import Router  # noqa: E402

import threading
LOCK = threading.Lock()
run.report = lambda *a, **k: None  # silence per-day lines
HOUR, DAY = 60, 1440
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def t(m):
    if m is None:
        return "-"
    return f"{DAYS[(m // DAY) % 7]}{'+' if m >= 7 * DAY else ''} {m % DAY // 60:02d}:{m % 60:02d}"


def analyze(events, verbose=True):
    orders = {}
    gig_order = {}
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    money = collections.defaultdict(lambda: collections.Counter())
    reqs = {}
    for e in events:
        k = e["kind"]
        if k == "price_request":
            reqs[e["request"]["id"]] = e["request"]
        elif k == "order_won":
            orders[e["order"]["id"]] = dict(e["order"], wonAt=e["at"])
        elif k == "order_changed":
            if e["order"]["id"] in orders:
                orders[e["order"]["id"]].update(e["order"])
                per[e["order"]["id"]]["changed"].append((e["at"], e["message"]["text"]))
        elif k == "order_cancelled":
            if e["orderId"] in orders:
                orders[e["orderId"]]["cancelled"] = True
        elif k == "ledger":
            line = e["line"]
            if line["orderId"]:
                money[line["orderId"]][line["kind"]] += line["cents"]
            else:
                money["_none"][line["kind"]] += line["cents"]
        elif k in ("picked_up", "delivered", "visit_failed"):
            per[e["orderId"]][k].append((e["at"], e["leg"], e.get("reason")))
            gig_order[e["gigId"]] = e["orderId"]
        elif k == "deadline_close":
            per[e["orderId"]]["deadline_close"].append(e["at"])
        elif k in ("gig_booked", "gig_dropped", "no_show", "gig_expired", "driver_late", "gig_started", "offer_answered"):
            per[("gig", e["gigId"])][k].append((e["at"], e.get("feeCents"), e.get("answer")))

    # link gigs to orders via ledger lines too
    for e in events:
        if e["kind"] == "ledger" and e["line"]["gigId"] and e["line"]["orderId"]:
            gig_order.setdefault(e["line"]["gigId"], e["line"]["orderId"])

    gigs_by_order = collections.defaultdict(list)
    for key, val in per.items():
        if isinstance(key, tuple):
            oid = gig_order.get(key[1])
            if oid:
                gigs_by_order[oid].append((key[1], val))

    cats = collections.Counter()
    late_rows = []
    for oid, o in orders.items():
        if o.get("cancelled"):
            continue
        deliv = per[oid]["delivered"]
        last = max((a for a, _, _ in deliv), default=None)
        legs_done = len({l for _, l, _ in deliv})
        undelivered = legs_done < len(o["legs"])
        late = undelivered or last > o["deadline"]
        if not late:
            cats["on_time"] += 1
            continue
        gigs = gigs_by_order.get(oid, [])
        books = sorted(b for _, g in gigs for b in g.get("gig_booked", []))
        drops = sum(len(g.get("gig_dropped", [])) + len(g.get("no_show", [])) for _, g in gigs)
        expired = sum(len(g.get("gig_expired", [])) for _, g in gigs)
        dlate = sum(len(g.get("driver_late", [])) for _, g in gigs)
        fails = per[oid]["visit_failed"]
        first_book = books[0][0] if books else None
        if undelivered:
            cat = "undelivered"
        elif fails:
            cat = "visit_failed:" + ",".join(sorted({f[2] for f in fails}))
        elif per[oid]["changed"]:
            cat = "order_changed"
        elif dlate:
            cat = "driver_late"
        elif drops:
            cat = "dropped"
        elif first_book is None or first_book > o["lastPickupAt"]:
            cat = "booked_too_late"
        else:
            cat = "booked_in_time_but_late"
        cats[cat] += 1
        r = reqs.get(oid, {})
        leg0 = o["legs"][0]
        late_rows.append((cat, oid, o["option"], t(o["wonAt"]), t(o["deadline"]), t(o["lastPickupAt"]),
                          t(first_book), t(last), drops, expired, len(fails),
                          leg0["pickup"]["placeId"], leg0["delivery"]["placeId"], len(o["legs"]),
                          leg0["pickup"]["privateAddress"] or leg0["delivery"]["privateAddress"],
                          r.get("referenceFeeCents", 0) // 100, o["priceCents"] // 100,
                          [b[1] // 100 for b in books]))
    total = collections.Counter()
    for oid, c in money.items():
        total.update(c)
    if verbose:
        late_rows.sort()
        print(f"{'cat':<26}{'order':<10}{'opt':<14}{'won':<10}{'deadline':<10}{'lastPU':<10}{'1stBook':<10}{'deliv':<10}dr ex vf  route  legs priv ref price fees")
        for row in late_rows:
            print(f"{row[0]:<26}{row[1]:<10}{row[2]:<14}{row[3]:<10}{row[4]:<10}{row[5]:<10}{row[6]:<10}{row[7]:<10}{row[8]:>2} {row[9]:>2} {row[10]:>2}  {row[11]}->{row[12]} {row[13]} {row[14]!s:<5} {row[15]:>4} {row[16]:>5} {row[17]}")
    return cats, {k: v / 100 for k, v in total.items()}


def _delete(world, tries=12):
    import time
    for _ in range(tries):
        try:
            world.delete()
            return
        except Exception:
            time.sleep(5)
    print(f"WARNING: could not delete {world.id}", file=sys.stderr)


def play_one(level, seed, verbose, keep, attempts=4):
    import time
    from spread.client import Unreachable
    client = Client()
    router = Router(client.map())
    for attempt in range(attempts):
        world = client.create(level=level, seed=seed)
        buf = io.StringIO()
        try:
            desk = run.play_practice(world, level, router)
            print(f"[{seed}] multi-car gigs posted: {desk.combo_count}", flush=True)
            tw = desk.pricer.dealer_wins
            print(f"[{seed}] trust: dealers {len(tw)}, total count {sum(tw.values())}, at 25: {sum(1 for v in tw.values() if v >= 25)}, avg {sum(tw.values()) / max(1, len(tw)):.1f}", flush=True)
            print(f"[{seed}] requests: " + ", ".join(f"{k}={v}" for k, v in desk.reasons.most_common()), flush=True)
            D = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            print(f"[{seed}] guard: " + ", ".join(f"{D[(t // 1440) % 7]} {t % 1440 // 60:02d}:{t % 60:02d} {s} ({r:.1%})" for t, s, r in desk.guard_log), flush=True)
            score = world.score()
            events = world.events()
            try:
                os.makedirs(os.path.join(ROOT, "tools", "runs"), exist_ok=True)
                with open(os.path.join(ROOT, "tools", "runs", f"reveal_L{level}_{seed}{os.environ.get('TAG', '')}.json"), "w") as fh:
                    json.dump(world.reveal(), fh)
            except Exception as e:
                print("reveal failed", e, file=sys.stderr)
            break
        except (Unreachable, TimeoutError, ConnectionError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(10)
        finally:
            if not keep:
                _delete(world)
    os.makedirs(os.path.join(ROOT, "tools", "runs"), exist_ok=True)
    with open(os.path.join(ROOT, "tools", "runs", f"L{level}_{seed}{os.environ.get('TAG', '')}.json"), "w") as fh:
        json.dump(events, fh)
    out = io.StringIO()
    with LOCK, contextlib.redirect_stdout(out):
        cats, money = analyze(events, verbose)
    return seed, score, cats, money, out.getvalue(), world.id


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--level", type=int, default=1)
    p.add_argument("--seeds", nargs="+", default=["s1"])
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--jobs", type=int, default=4)
    a = p.parse_args()
    results = []
    with cf.ThreadPoolExecutor(min(a.jobs, 7)) as ex:
        futs = [ex.submit(play_one, a.level, s, not a.quiet, a.keep) for s in a.seeds]
        for f in cf.as_completed(futs):
            seed, score, cats, money, text, wid = f.result()
            results.append((seed, score, cats, money))
            if text.strip():
                print(f"===== seed {seed} ({wid}) =====")
                print(text)
    tot_m = 0
    for seed, score, cats, money in sorted(results):
        tot_m += score["marginCents"]
        cs = ", ".join(f"{k}={v}" for k, v in cats.most_common() if k != "on_time")
        print(f"L{a.level} {seed:<8} margin €{score['marginCents']/100:>10,.2f}  orders {score['wonOrders']:>4}  on-time {score['onTimeShare']:6.1%}  undeliv {score['undeliveredShare']:5.1%}  | {cs}")
        print(f"          money: " + ", ".join(f"{k}={v:,.0f}" for k, v in sorted(money.items())))
    print(f"L{a.level} total margin €{tot_m/100:,.2f}  min on-time {min(r[1]['onTimeShare'] for r in results):.1%}")


if __name__ == "__main__":
    main()

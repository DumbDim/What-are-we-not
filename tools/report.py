"""Where and when our orders, money and declines happen, averaged over saved runs.

Usage: python tools/report.py m1 m2 m3 [--tag _rep] [--level 8]
"""
import argparse
import collections
import json
import os

RUNS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def hhmm(m):
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def day_name(t):
    d = t // 1440
    return DAYS[d % 7] + ("+" if d >= 7 else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds", nargs="+")
    ap.add_argument("--tag", default="_rep")
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    n = len(a.seeds)

    cars_to = collections.Counter()  # delivery place -> cars delivered
    time_to = collections.defaultdict(list)  # delivery place -> minute of day of delivery
    orders_to = collections.Counter()  # delivery place of won orders
    orders_from = collections.Counter()  # pickup place of won orders
    margin_to = collections.Counter()  # delivery place -> margin cents
    margin_from = collections.Counter()  # pickup place -> margin cents
    reject_from = collections.Counter()  # pickup place of requests we declined
    reject_hour = collections.Counter()  # hour of day we declined
    lost_from = collections.Counter()  # pickup place of quotes the competitor won
    req_hour = collections.Counter()  # hour of day requests arrive
    won_hour = collections.Counter()
    req_dayhour = collections.Counter()
    margin_day_booked = collections.Counter()  # by the day the money line landed
    margin_day_won = collections.Counter()  # by the day we won the order
    won_day = collections.Counter()
    reasons = collections.Counter()
    scores = []

    for seed in a.seeds:
        ev = json.load(open(os.path.join(RUNS, f"L{a.level}_{seed}{a.tag}.json")))
        rv = json.load(open(os.path.join(RUNS, f"reveal_L{a.level}_{seed}{a.tag}.json")))
        reqs = {e["request"]["id"]: e["request"] for e in ev if e["kind"] == "price_request"}
        won = {e["order"]["id"]: (e["at"], e["order"]) for e in ev if e["kind"] == "order_won"}
        cancelled = {e["orderId"] for e in ev if e["kind"] == "order_cancelled"}
        quotes = {r["request"]["id"]: r["quote"] for r in rv["requests"]}

        for rid, r in reqs.items():
            h = r["arrivedAt"] % 1440 // 60
            req_hour[h] += 1
            req_dayhour[(r["arrivedAt"] // 1440, h)] += 1
            q = quotes.get(rid)
            lp = min(o["listPriceCents"] for o in r["options"])
            declined = not q or min(q.values()) >= 3 * lp
            pickup = r["legs"][0]["pickup"]["placeId"]
            if declined:
                reject_from[pickup] += 1
                reject_hour[h] += 1
            elif rid not in won:
                lost_from[pickup] += 1

        order_margin = collections.Counter()
        for e in ev:
            if e["kind"] == "ledger":
                line = e["line"]
                margin_day_booked[e["at"] // 1440] += line["cents"]
                if line["orderId"]:
                    order_margin[line["orderId"]] += line["cents"]
            elif e["kind"] == "delivered" and e["orderId"] not in cancelled:
                cars_to[e["placeId"]] += 1
                time_to[e["placeId"]].append(e["at"] % 1440)

        for oid, (at, o) in won.items():
            if oid in cancelled:
                continue
            leg = o["legs"][0]
            pickup, delivery = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
            orders_from[pickup] += 1
            orders_to[delivery] += 1
            margin_from[pickup] += order_margin[oid]
            margin_to[delivery] += order_margin[oid]
            margin_day_won[at // 1440] += order_margin[oid]
            won_day[at // 1440] += 1
            won_hour[at % 1440 // 60] += 1

        for line in open(os.path.join(os.path.dirname(RUNS), f"rep_L{a.level}.txt"), encoding="utf-8", errors="replace"):
            if line.startswith(f"[{seed}] requests:"):
                for part in line.split(":", 1)[1].split(","):
                    k, _, v = part.strip().partition("=")
                    if v.isdigit():
                        reasons[k] += int(v)
            if line.startswith(f"L{a.level} {seed} "):
                scores.append(line.strip())

    def top(counter, k=a.top, money=False, per=True):
        rows = counter.most_common(k)
        return "\n".join(
            f"   {place:<14} {(v / n / 100 if money else v / n):>10,.1f}{' €' if money else ''}" for place, v in rows)

    print(f"Averages per week over {n} level-{a.level} weeks ({', '.join(a.seeds)})\n")
    print("Scores:")
    for s in scores:
        print("  ", s)

    print("\n1. Cars delivered to each place (per week) and average time of day they arrive")
    for place, c in cars_to.most_common():
        times = time_to[place]
        print(f"   {place:<14} {c / n:>6.1f} cars   avg arrival {hhmm(sum(times) / len(times))}")

    print("\n2. Places that make us the most money (margin per week)")
    print("  by delivery place:")
    print(top(margin_to, money=True))
    print("  by pickup place:")
    print(top(margin_from, money=True))
    print("  least profitable delivery places:")
    for place, v in sorted(margin_to.items(), key=lambda kv: kv[1])[:5]:
        print(f"   {place:<14} {v / n / 100:>10,.1f} €")

    print("\n3. Most orders TO (delivery place, orders per week)")
    print(top(orders_to))
    print("   Most orders FROM (pickup place, orders per week)")
    print(top(orders_from))

    print("\n4. Where we decline the most requests (pickup place, per week)")
    print(top(reject_from))
    print("   Why we decline (per week): " + ", ".join(f"{k}={v / n:.0f}" for k, v in reasons.most_common()))
    print("   Hour of day we decline the most (declines per week):")
    for h, v in sorted(reject_hour.items(), key=lambda kv: -kv[1])[:6]:
        print(f"   {h:02d}:00-{h:02d}:59  {v / n:>6.1f}")
    print("   Where the competitor beats us the most (pickup place, lost quotes per week)")
    print(top(lost_from, k=5))

    print("\n5. When requests arrive (per week, by hour of day)   [won orders in brackets]")
    for h in range(24):
        if req_hour[h]:
            bar = "#" * int(req_hour[h] / n / 5)
            print(f"   {h:02d}:00  {req_hour[h] / n:>6.1f}  [{won_hour[h] / n:>5.1f}]  {bar}")
    print("   Busiest weekday hours (requests per week):")
    for (d, h), v in req_dayhour.most_common(5):
        print(f"   {DAYS[d % 7]} {h:02d}:00  {v / n:.1f}")

    print("\n6. Money by day (per week)")
    print("   by the day we WON the order: orders and the margin those orders made")
    for d in sorted(margin_day_won):
        print(f"   {DAYS[d % 7]:<4} {won_day[d] / n:>6.1f} orders  €{margin_day_won[d] / n / 100:>9,.0f}")
    print("   by the day the money was booked (revenue lands on delivery):")
    for d in sorted(margin_day_booked):
        print(f"   {day_name(d * 1440):<5} €{margin_day_booked[d] / n / 100:>9,.0f}")


if __name__ == "__main__":
    main()

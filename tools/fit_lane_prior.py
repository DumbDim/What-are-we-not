"""Fit a per-lane competitor price table from saved level-8 quote outcomes (reveals).

The competitor's lane factor is partly the same in every world (HANDBOOK). For each of our quotes we know
price/list and whether it won. Fit logit P(win) = a0 + p[pickup] + d[delivery] + l[lane] + b * ratio with a
shared slope b, then turn each lane's 50% point into a price factor relative to the average lane.

Usage: python tools/fit_lane_prior.py --exclude m1 m2 m3
Writes dispatcher/lane_prior.json  {"pickup|delivery": factor}
"""
import argparse
import glob
import json
import math
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--epochs", type=int, default=6)
    a = ap.parse_args()
    rows = []
    files = sorted(glob.glob(os.path.join(ROOT, "tools", "runs", "reveal_L8_*.json")))
    used = 0
    for f in files:
        seed = os.path.basename(f)[len("reveal_L8_"):-5].split("_")[0]
        if seed in a.exclude:
            continue
        try:
            rv = json.load(open(f))
        except Exception:
            continue
        used += 1
        for r in rv["requests"]:
            q, rq = r["quote"], r["request"]
            if not q:
                continue
            first = next((o for o in rq["options"] if o["kind"] in q), None)
            if first is None:
                continue
            ratio = q[first["kind"]] / first["listPriceCents"]
            if ratio >= 3 or ratio <= 0.5:
                continue  # declines and odd quotes
            leg = rq["legs"][0]
            rows.append((leg["pickup"]["placeId"], leg["delivery"]["placeId"], ratio, r["outcome"] == "won"))
    print(f"reveals used {used}, quotes {len(rows)}")
    w = {"a0": 8.6, "b": -6.0}
    lr, l2 = 0.02, {"p": 0.002, "d": 0.002, "l": 0.02}
    random.seed(1)
    for ep in range(a.epochs):
        random.shuffle(rows)
        loss = 0.0
        for p, d, r, y in rows:
            kp, kd, kl = "p:" + p, "d:" + d, "l:" + p + "|" + d
            z = w["a0"] + w.get(kp, 0) + w.get(kd, 0) + w.get(kl, 0) + w["b"] * r
            pr = 1 / (1 + math.exp(-max(-30, min(30, z))))
            loss += -math.log(pr if y else 1 - pr)
            g = (1.0 if y else 0.0) - pr
            w["a0"] += lr * g
            # slope fixed at the measured steepness: our prices follow the lanes, so it is not identified here
            for k, t in ((kp, "p"), (kd, "d"), (kl, "l")):
                w[k] = w.get(k, 0) + lr * (g - l2[t] * w.get(k, 0))
        print(f"epoch {ep}: log-loss {loss / len(rows):.4f}  slope {w['b']:.2f}")
    # 50% point of each lane, relative to the average lane
    b, a0 = w["b"], w["a0"]
    r50_global = -a0 / b
    lanes = {}
    places = sorted({k[2:] for k in w if k.startswith("p:")} | {k[2:] for k in w if k.startswith("d:")})
    for p in places:
        for d in places:
            off = w.get("p:" + p, 0) + w.get("d:" + d, 0) + w.get("l:" + p + "|" + d, 0)
            if off == 0:
                continue
            r50 = -(a0 + off) / b
            lanes[p + "|" + d] = round(max(0.85, min(1.20, r50 / r50_global)), 4)
    out = os.path.join(ROOT, "dispatcher", "lane_prior.json")
    json.dump({"r50_global": round(r50_global, 4), "lanes": lanes}, open(out, "w"), indent=0)
    vals = sorted(lanes.values())
    print(f"global 50% point {r50_global:.2f}x list; {len(lanes)} lanes; factor p10 {vals[len(vals)//10]:.2f} median {vals[len(vals)//2]:.2f} p90 {vals[9*len(vals)//10]:.2f}")
    print("wrote", out)


if __name__ == "__main__":
    main()

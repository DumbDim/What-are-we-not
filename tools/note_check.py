"""Score the note reader against reveals: python tools/note_check.py tools/runs/reveal_*.json [-v]"""
import sys, os, json, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dispatcher.notes import read_note

DAY = 1440
verbose = "-v" in sys.argv
score = collections.Counter()
misses = collections.defaultdict(list)
for f in sorted(sum([glob.glob(a) for a in sys.argv[1:] if a != "-v"], [])):
    rev = json.load(open(f))
    reqs = {r["request"]["id"]: r["request"] for r in rev["requests"]}
    for o in rev["orders"]:
        r = reqs.get(o["orderId"])
        if r is None:
            continue
        text = (r.get("note") or {}).get("text")
        facts = read_note(text, r["arrivedAt"])
        t0 = o["legs"][0]
        leg0 = r["legs"][0]

        def check(name, truth, got):
            score[(name, truth == got)] += 1
            if truth != got:
                misses[name].append((text, truth, got))

        check("plates", bool(t0["needsPlates"]), facts.plates)
        check("manual", bool(t0["manualGearbox"]), facts.manual)
        # ready later than the request said (or released after payment)
        stated = leg0["pickup"]["window"]["from"]
        real = max(t0["readyAt"] or 0, t0["releasedAt"] or 0)
        truth_late = real > stated
        got_from = facts.pickup_from if facts.pickup_from is not None and facts.pickup_from > stated else None
        check("ready_later", (real if truth_late else None), got_from)
        stated_hours = leg0["pickup"]["hours"]
        if stated_hours and t0["pickupHours"]:
            th = t0["pickupHours"]
            truth_h = None if (th["opens"], th["closes"]) == (stated_hours["opens"], stated_hours["closes"]) else (th["opens"], th["closes"])
            got_h = None
            if facts.pickup_hours:
                o_, c_ = max(stated_hours["opens"], facts.pickup_hours[0]), min(stated_hours["closes"], facts.pickup_hours[1])
                got_h = None if (o_, c_) == (stated_hours["opens"], stated_hours["closes"]) else (o_, c_)
            check("pickup_hours", truth_h, got_h)
        dstated = r["legs"][0]["delivery"]["hours"]
        if dstated and t0["deliveryHours"]:
            th = t0["deliveryHours"]
            truth_h = None if (th["opens"], th["closes"]) == (dstated["opens"], dstated["closes"]) else (th["opens"], th["closes"])
            got_h = None
            if facts.delivery_hours:
                o_, c_ = max(dstated["opens"], facts.delivery_hours[0]), min(dstated["closes"], facts.delivery_hours[1])
                got_h = None if (o_, c_) == (dstated["opens"], dstated["closes"]) else (o_, c_)
            check("delivery_hours", truth_h, got_h)
        check("note_deadline", t0["noteDeadline"], facts.delivery_by)

names = sorted({k[0] for k in score})
for n in names:
    ok, bad = score[(n, True)], score[(n, False)]
    print(f"{n:<15} correct {ok:>5}  wrong {bad:>4}  ({ok / max(1, ok + bad):.1%})")
if verbose:
    for n, rows in misses.items():
        print(f"\n== {n} ({len(rows)})")
        for text, truth, got in rows[:25]:
            print(f"  truth={truth} got={got} | {text}")

"""Win every request in a level-8 practice world (quote 1 cent), post nothing, run to the end, save the reveal,
which shows the truth behind every note. Usage: python tools/note_corpus.py seed [--world ID]"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run
from spread.client import Client, Unreachable
c = Client()
seed = sys.argv[1]
w = c.world(sys.argv[3]) if "--world" in sys.argv else c.create(level=8, seed=seed)
snap = w.snapshot(); end = snap["endsAt"]; now = snap["now"]
actions = [{"type": "quote", "requestId": r["id"], "prices": {o["kind"]: 1 for o in r["options"]}} for r in snap["openRequests"]]
while now < end:
    try:
        if now < 5 * 1440:
            step = w.advance(until=["price_request"], max=min(5 * 1440, now + 120), actions=actions or None) if actions else w.advance(until=["price_request"], max=min(5 * 1440, now + 120))
        else:
            step = w.advance(to=min(end, now + 360))
    except Unreachable as e:
        print("retry", e); time.sleep(5); now = w.snapshot()["now"]; continue
    actions = []
    now = step["now"]
    for e in step["events"]:
        if e["kind"] == "price_request":
            r = e["request"]
            actions.append({"type": "quote", "requestId": r["id"], "prices": {o["kind"]: 1 for o in r["options"]}})
rev = w.reveal()
json.dump(rev, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", f"notes_{seed}.json"), "w"))
print(seed, w.id, len(rev["requests"]), len(rev["orders"]))

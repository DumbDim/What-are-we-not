"""Re-analyze dumped runs: python tools/redo.py tools/runs/L1_*.json [-v]"""
import sys, os, json, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import analyze
verbose = "-v" in sys.argv
for f in sorted(sum([glob.glob(a) for a in sys.argv[1:] if a != "-v"], [])):
    ev = json.load(open(f))
    cats, money = analyze(ev, verbose)
    n = sum(cats.values()); ok = cats["on_time"]
    print(f"{os.path.basename(f):<16} orders {n:>4} on-time {ok/max(1,n):6.1%} margin €{sum(money.values()):>10,.0f} | " + ", ".join(f"{k}={v}" for k, v in cats.most_common() if k != "on_time"))

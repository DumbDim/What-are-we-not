import sys,os; sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dispatcher.notes import read_note
D=1440
fmt=lambda m: None if m is None else f"d{m//D} {m%D//60:02d}:{m%60:02d}"
for t in sys.stdin.read().split("\n"):
    if not t.strip(): continue
    f=read_note(t, 0*D+600)
    print(t[:60].ljust(60),'PL' if f.plates else '','MAN' if f.manual else '','summer' if f.summer_tyres else '','from',fmt(f.pickup_from),'pH',f.pickup_hours,'by',fmt(f.delivery_by),'dH',f.delivery_hours)

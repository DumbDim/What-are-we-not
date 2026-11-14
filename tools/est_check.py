import json,glob,sys,os
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run
from spread.client import Client
from dispatcher.routing import Router
from dispatcher.timing import estimate_last_pickup
r=Router(Client().map())
diffs=[];bad=[]
for f in sum([glob.glob(a) for a in sys.argv[1:]],[]):
    ev=json.load(open(f))
    reqs={e['request']['id']:e['request'] for e in ev if e['kind']=='price_request'}
    for e in ev:
        if e['kind']!='order_won': continue
        o=e['order']; rq=reqs[o['id']]
        opt=[x for x in rq['options'] if x['kind']==o['option']][0]
        est=estimate_last_pickup(rq['legs'],o['deadline'],r)
        d=est-o['lastPickupAt']; diffs.append(d)
        if abs(d)>60: bad.append((d,o['id'],len(o['legs']),[(l['pickup']['privateAddress'],l['delivery']['privateAddress']) for l in o['legs']], opt['deadline'], o['deadline'], o['lastPickupAt'], (rq['note'] or {}).get('text')))
diffs.sort(); n=len(diffs)
print(n,'exact',sum(d==0 for d in diffs),'within30',sum(abs(d)<=30 for d in diffs),'p5',diffs[n//20],'p50',diffs[n//2],'p95',diffs[-n//20])
for b in bad[:20]: print(b)

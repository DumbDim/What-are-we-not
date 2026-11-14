import json,glob,sys,os,collections
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run
from spread.client import Client
from dispatcher.routing import Router
from dispatcher.timing import awake_minutes
r=Router(Client().map())
B=collections.defaultdict(lambda:[0,0])
for f in sum([glob.glob(a) for a in sys.argv[1:]],[]):
    ev=json.load(open(f))
    canc={e['orderId'] for e in ev if e['kind']=='order_cancelled'}
    dl=collections.defaultdict(list)
    for e in ev:
        if e['kind']=='delivered': dl[e['orderId']].append(e['at'])
    for e in ev:
        if e['kind']!='order_won' or e['order']['id'] in canc: continue
        o=e['order']; l=o['legs'][0]
        drive=r.drive_minutes(l['pickup']['placeId'],l['delivery']['placeId'])
        lead=awake_minutes(e['at'],o['lastPickupAt'])
        ok=len(dl[o['id']])>=len(o['legs']) and max(dl[o['id']])<=o['deadline']
        key=(o['option'], 'drive<2h' if drive<120 else 'drive2-4h' if drive<240 else 'drive4h+', 'lead<6h' if lead<360 else 'lead6-10h' if lead<600 else 'lead10h+')
        B[key][0]+=1; B[key][1]+=ok
for k in sorted(B): print(k, B[k][0], f"{B[k][1]/B[k][0]:.1%}")

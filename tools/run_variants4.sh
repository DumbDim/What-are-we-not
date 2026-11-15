#!/bin/bash
# Win model with improvements a-d (combo3 extras off), and all of them with combo3, on m2 and m3.
cd "$(dirname "$0")/.."
OFF='"FLOOR_QUOTE": false, "HOT_PRICE": 1.0, "HOT_CAP_SHARE": 0.0, "LOW_DEST_MARKUP": 1.0'
declare -A V
V[ac]="{\"MODEL_PRICING\": true, \"MODEL_LATE\": true, \"MODEL_TRUST_REGION\": 0.10, $OFF}"
V[acb]="{\"MODEL_PRICING\": true, \"MODEL_LATE\": true, \"MODEL_TRUST_REGION\": 0.10, \"MODEL_LANE_FEE\": true, $OFF}"
V[acbd]="{\"MODEL_PRICING\": true, \"MODEL_LATE\": true, \"MODEL_TRUST_REGION\": 0.10, \"MODEL_LANE_FEE\": true, \"MODEL_OPTIONS\": true, $OFF}"
V[allcombo]='{"MODEL_PRICING": true, "MODEL_LATE": true, "MODEL_TRUST_REGION": 0.10, "MODEL_LANE_FEE": true, "MODEL_OPTIONS": true}'
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds m2 m3 --quiet --jobs 2 > "tools/var4_$1.txt" 2>&1; }
run ac & run acb & run acbd & run allcombo & wait
echo "== combo3 (committed)"; grep -E "^L8 m[23]" tools/var2_combo3.txt
echo "== model (plain)"; grep -E "^L8 m[23]" tools/var3_model.txt
for v in ac acb acbd allcombo; do echo "== $v"; grep -E "^L8|model:|Error|Traceback" "tools/var4_$v.txt"; done

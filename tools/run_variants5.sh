#!/bin/bash
# Max-volume variations on m2 and m3.
cd "$(dirname "$0")/.."
LOOSE='"LEAD_MIN": {"8": 240}, "L8_BUFFER": 180, "MIN_DRIVERS": {"8": 2}, "STUCK_STOP": 6, "CAP_SHARE": 0.10, "CAP_SHARE_RICH": 0.13, "EXPECTED_FEE": 1.1, "MIN_MARGIN": 0.05'
FEES='"FEE_HOLD_SHARE": 0.0, "FEE_PLATEAU": 1.8'
declare -A V
V[v1]="{$LOOSE, \"MODEL_PRICING\": true}"
V[v2]="{$LOOSE, \"MODEL_PRICING\": true, $FEES}"
V[v3]="{$LOOSE, $FEES}"
V[v4]="{$LOOSE, $FEES, \"MODEL_PRICING\": true, \"MODEL_LATE\": true, \"LATE_LOAD\": 0.15, \"CAP_LAMBDA\": 0.25, \"MODEL_LANE_FEE\": true}"
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds m2 m3 --quiet --jobs 2 > "tools/var5_$1.txt" 2>&1; }
run v1 & run v2 & run v3 & run v4 & wait
echo "== combo3 (committed)"; grep -E "^L8 m[23]" tools/var2_combo3.txt
for v in v1 v2 v3 v4; do echo "== $v"; grep -E "^L8|money|Error|Traceback" "tools/var5_$v.txt"; done

#!/bin/bash
# Spend the fee step's on-time headroom on volume. m2, m3.
cd "$(dirname "$0")/.."
VIEW='"VIEWER_OFFERS": true, "OLD_OFFERS": false, "SPEED_CAP": true, "SAFETY_MIN": 10, "SAFETY_SHARE": 0.03'
STEP='"FEE_STEP_AFTER": 45, "FEE_STEP": 1.3'
MID='"CAP_SHARE": 0.08, "CAP_SHARE_RICH": 0.10, "STUCK_STOP": 4'
WIDE='"CAP_SHARE": 0.10, "CAP_SHARE_RICH": 0.13, "STUCK_STOP": 5, "LEAD_MIN": {"8": 240}, "MIN_DRIVERS": {"8": 2}'
declare -A V
V[x1]="{$VIEW, $STEP, $MID}"
V[x2]="{$VIEW, $STEP, $WIDE}"
V[x3]="{$VIEW, $MID}"
V[x4]="{$VIEW, $STEP, $MID, \"MODEL_PRICING\": true}"
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds m2 m3 --quiet --jobs 2 > "tools/var7_$1.txt" 2>&1; }
run x1 & run x2 & run x3 & run x4 & wait
echo "== combo3 (committed)"; grep -E "^L8 m[23]" tools/var2_combo3.txt
for v in x1 x2 x3 x4; do echo "== $v"; grep -E "^L8|money|Error|Traceback" "tools/var7_$v.txt"; done

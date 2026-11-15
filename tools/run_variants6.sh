#!/bin/bash
# Viewer offers + old offers off + booking-speed capacity, with the fee step and smaller buffers as options. m2, m3.
cd "$(dirname "$0")/.."
BASE='"VIEWER_OFFERS": true, "OLD_OFFERS": false, "SPEED_CAP": true'
STEP='"FEE_STEP_AFTER": 45, "FEE_STEP": 1.3'
BUF='"SAFETY_MIN": 10, "SAFETY_SHARE": 0.03'
declare -A V
V[w1]="{$BASE}"
V[w2]="{$BASE, $STEP}"
V[w3]="{$BASE, $BUF}"
V[w4]="{$BASE, $STEP, $BUF}"
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds m2 m3 --quiet --jobs 2 > "tools/var6_$1.txt" 2>&1; }
run w1 & run w2 & run w3 & run w4 & wait
echo "== combo3 (committed)"; grep -E "^L8 m[23]" tools/var2_combo3.txt
for v in w1 w2 w3 w4; do echo "== $v"; grep -E "^L8|money|viewer offers|Error|Traceback" "tools/var6_$v.txt"; done

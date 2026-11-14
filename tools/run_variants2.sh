#!/bin/bash
# Each data-driven change alone and together, on the m1-m3 baseline seeds.
cd "$(dirname "$0")/.."
declare -A V
V[floor]='{"FLOOR_QUOTE": true}'
V[hot]='{"HOT_PRICE": 0.93, "HOT_CAP_SHARE": 0.09}'
V[remote]='{"LOW_DEST_MARKUP": 1.25}'
V[combo3]='{"FLOOR_QUOTE": true, "HOT_PRICE": 0.93, "HOT_CAP_SHARE": 0.09, "LOW_DEST_MARKUP": 1.25}'
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds m1 m2 m3 --quiet --jobs 3 > "tools/var2_$1.txt" 2>&1; }
run floor & run hot & wait
run remote & run combo3 & wait
echo "== baseline"; grep -E "^L8" tools/rep_L8.txt
for v in floor hot remote combo3; do echo "== $v"; grep -E "^L8|Error|Traceback" "tools/var2_$v.txt"; done

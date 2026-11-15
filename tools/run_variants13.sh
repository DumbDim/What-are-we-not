#!/bin/bash
# Lane price table, bulk-order premium and dealer win-share, each alone, on m1-m3.
cd "$(dirname "$0")/.."
run() { TAG="_$1" SPREAD_TUNE="$2" python -u tools/analyze.py --level 8 --seeds m1 m2 m3 --quiet --jobs 3 > "tools/var13_$1.txt" 2>&1; }
run prior '{"LANE_PRIOR": true}' & run bulk '{"BULK_ON": true}' & wait
run dealer '{"DEALER_SHARE_ON": true}'
echo "== combo3 (current)"; grep -E "^L8" tools/var2_combo3.txt
for v in prior bulk dealer; do echo "== $v"; grep -E "^L8|Error|Traceback" "tools/var13_$v.txt"; done

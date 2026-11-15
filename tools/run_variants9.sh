#!/bin/bash
# Destination priority: acceptance (P1), fees (P2), both (P3).
cd "$(dirname "$0")/.."
ACC='"PRIO_CAP_BOOST": 1.5, "PRIO_LEAD_CUT": 60, "LOW_CAP_CUT": 0.5, "LOW_LEAD_ADD": 120'
FEE='"PRIO_FEE": true'
run() { TAG="_$1" SPREAD_TUNE="$2" python -u tools/analyze.py --level 8 --seeds $3 --quiet --jobs 3 > "tools/var9_$1.txt" 2>&1; }
run p1 "{$ACC}" "m2 m3" & run p2 "{$FEE}" "m2 m3" & run p3 "{$ACC, $FEE}" "m1 m2 m3" & wait
echo "== combo3 (committed)"; grep -E "^L8" tools/var2_combo3.txt
for v in p1 p2 p3; do echo "== $v"; grep -E "^L8|Error|Traceback" "tools/var9_$v.txt"; done

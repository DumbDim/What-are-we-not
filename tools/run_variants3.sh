#!/bin/bash
# Win-model pricing alone (combo3 extras off) and combined with combo3, on m1-m3.
cd "$(dirname "$0")/.."
declare -A V
V[model]='{"MODEL_PRICING": true, "FLOOR_QUOTE": false, "HOT_PRICE": 1.0, "HOT_CAP_SHARE": 0.0, "LOW_DEST_MARKUP": 1.0}'
V[modelcombo]='{"MODEL_PRICING": true}'
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds m1 m2 m3 --quiet --jobs 3 > "tools/var3_$1.txt" 2>&1; }
run model & run modelcombo & wait
echo "== combo3 (committed)"; grep -E "^L8" tools/var2_combo3.txt
for v in model modelcombo; do echo "== $v"; grep -E "^L8|model:|Error|Traceback" "tools/var3_$v.txt"; done

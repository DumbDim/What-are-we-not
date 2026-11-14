#!/bin/bash
# Safe (committed) settings, then each idea on top. Usage: bash tools/run_variants.sh
cd "$(dirname "$0")/.."
SAFE='"LEAD_MIN": {"8": 300}, "L8_BUFFER": 360, "MIN_DRIVERS": {"8": 4}, "STUCK_STOP": 3, "CAP_SHARE": 0.05, "CAP_SHARE_RICH": 0.06, "CAP_RICH": 100, "EXPECTED_FEE": 1.2, "MIN_MARGIN": 0.10, "RISK_TIGHT": 9.0, "COMBO_LEVEL": 99'
declare -A V
V[safe]="{$SAFE}"
V[war]="{$SAFE, \"WAR_PRICE\": 0.82}"
V[monday]="{$SAFE, \"MONDAY_MARKUP\": 1.30}"
V[rich]="{$SAFE, \"CAP_RICH\": 150, \"CAP_SHARE_RICH\": 0.10}"
V[all3]="{$SAFE, \"WAR_PRICE\": 0.82, \"MONDAY_MARKUP\": 1.30, \"CAP_RICH\": 150, \"CAP_SHARE_RICH\": 0.10}"
run() { TAG="_$1" SPREAD_TUNE="${V[$1]}" python -u tools/analyze.py --level 8 --seeds k1 h1 --quiet --jobs 2 > "tools/var_$1.txt" 2>&1; }
run safe & run war & run monday & run rich & wait
run all3
for v in safe war monday rich all3; do echo "== $v"; grep -E "^L8|Error|Traceback" "tools/var_$v.txt"; done

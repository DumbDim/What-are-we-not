#!/bin/bash
# Scale on next-day work: S1 option steering, S2 same-day fee + viewer offers, S3 both + later-order cap weight.
cd "$(dirname "$0")/.."
S1='"OPTION_ADJ": {"same_day": 1.10, "next_day": 1.0, "within_3_days": 0.95}'
S2='"SAMEDAY_OPEN": 1.2, "VIEWER_OFFERS": true, "OLD_OFFERS": false, "VIEWER_SAMEDAY_ONLY": true'
S3='"LATER_CAP_WEIGHT": 0.5'
run() { TAG="_$1" SPREAD_TUNE="$2" python -u tools/analyze.py --level 8 --seeds $3 --quiet --jobs 3 > "tools/var10_$1.txt" 2>&1; }
run s1 "{$S1}" "m2 m3" & run s2 "{$S2}" "m2 m3" & run s3 "{$S1, $S2, $S3}" "m1 m2 m3" & wait
echo "== combo3 (committed)"; grep -E "^L8" tools/var2_combo3.txt
for v in s1 s2 s3; do echo "== $v"; grep -E "^L8|Error|Traceback" "tools/var10_$v.txt"; done

#!/bin/bash
# The submitted final (b1f4b51): one week at each level 1-8 on seed z1, then 8 level-8 weeks y1-y8.
cd "$(dirname "$0")/.."
export SPREAD_TUNE=
for L in 1 2 3 4 5 6 7 8; do TAG=_sub python -u tools/analyze.py --level $L --seeds z1 --quiet --jobs 1 > tools/sub_L$L.txt 2>&1 & done
wait
TAG=_sub python -u tools/analyze.py --level 8 --seeds y1 y2 y3 y4 y5 y6 y7 y8 --quiet --jobs 8 > tools/sub_L8_8weeks.txt 2>&1
echo "== levels 1-8, seed z1"
for L in 1 2 3 4 5 6 7 8; do grep -hE "^L$L z1|Error|Traceback" tools/sub_L$L.txt; done
echo "== level 8, seeds y1-y8"
grep -E "^L8|Error|Traceback" tools/sub_L8_8weeks.txt

#!/bin/bash
# GLM-5.3-Flash live checks of VRAM Lend (live.py), as run for results/glm.md.
#
# Environment:
#   MODEL5      model id of a five-GPU GLM entry (required)
#   MODEL3      model id of a three-GPU GLM entry (optional: the prompt check at full lendable)
#   GPU         PCI address for the 8 GiB checks (default 0000:09:00.0)
#   PROMPT_GPU  PCI address for the three-GPU prompt check (default 0000:05:00.0)
#   ENGINE_LOG3 the three-GPU engine's log file (the prompt check reads it)
#   OUT         results file (default results/glm-run.md)
#   PYTHON      Python with requests (default python3)
#   VRAM_LEND_BASE_URL  the server (live.py default http://127.0.0.1:8080)
set -u
: "${MODEL5:?set MODEL5 to the five-GPU model id}"
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}; OUT=${OUT:-$HERE/results/glm-run.md}
GPU=${GPU:-0000:09:00.0}; PROMPT_GPU=${PROMPT_GPU:-0000:05:00.0}
"$PY" "$HERE/live.py" "$MODEL5" "$OUT" --checks state,big,each,over,two,busy,while_requests,cost3,restart \
  --gpu "$GPU" --cost-n 6
if [ -n "${MODEL3:-}" ]; then
  "$PY" "$HERE/live.py" "$MODEL3" "$OUT" --checks state,each,prompt --gpu "$PROMPT_GPU" \
    --prompt-gpu "$PROMPT_GPU" --prompt-words 4030 --engine-log "${ENGINE_LOG3:-}"
fi

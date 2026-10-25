#!/bin/bash
# DeepSeek-V4.1 live checks of VRAM Lend (live.py), as run for results/deepseek.md.
#
# Environment:
#   MODEL       model id of a five-GPU DeepSeek entry (required)
#   GPU         PCI address for the checks (default 0000:09:00.0)
#   ENGINE_LOG  the engine's log file (the prompt and late checks read it)
#   OUT         results file (default results/deepseek-run.md)
#   PYTHON      Python with requests (default python3)
#   VRAM_LEND_BASE_URL  the server (live.py default http://127.0.0.1:8080)
set -u
: "${MODEL:?set MODEL to the DeepSeek model id}"
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}; OUT=${OUT:-$HERE/results/deepseek-run.md}
GPU=${GPU:-0000:09:00.0}
"$PY" "$HERE/live.py" "$MODEL" "$OUT" \
  --checks state,big,each,over,two,busy,while_requests,prompt,restart --gpu "$GPU" \
  --prompt-gpu "$GPU" --prompt-words 4030 --engine-log "${ENGINE_LOG:-}"
# a lend during a prompt read that takes longer than the server's 30 s wait: an 80K-token prompt (about one token a
# word). With the default 16000 words the lend is answered inside the wait and the check fails.
"$PY" "$HERE/live.py" "$MODEL" "$OUT" --checks late --gpu "$GPU" --late-words 80000 --engine-log "${ENGINE_LOG:-}"
# decode t/s before, during and after an 8 GiB lend, after a warm-up
"$PY" "$HERE/live.py" "$MODEL" "$OUT" --checks cost3 --gpu "$GPU" --cost-n 10 --warm 4

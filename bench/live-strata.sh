#!/bin/bash
# Strata live checks of VRAM Lend (live.py) on a two-GPU layer split, served through llama-swap.
#
# Usage: live-strata.sh run1
#   run1: on each GPU in GPUS: RUNS runs of the 8 GiB lend and return while a request streams, a 2 GiB lend on each
#         GPU in turn (the other GPUs stay as they were), a lend of the full lendable and its return, a lend over
#         lendable, two leases returned in order, and a return while another process holds the memory. Then one
#         engine restart under the same server (no leases after it, the old id answers 404). The model is unloaded
#         at the end.
#
# Environment:
#   MODEL         model id of the Strata entry with VRAM Lend on (default swift15-flash-next-iq4xs-strata-2x3090-vramlend)
#   GPUS          PCI addresses of the GPUs to check (default "0000:01:00.0 0000:02:00.0")
#   RUNS          runs of the 8 GiB check per GPU (default 3)
#   SEGMENT_MIB   the engine's lend segment size; Strata's /vram/state gives none (default 512)
#   ENGINE_MATCH  regex on pgrep -af lines of the engine process (default: the Strata binary, build/strata)
#   OUT_DIR       directory for the result files (default results/strata)
#   UNLOAD        1 unloads the model at the end through llama-swap (default 1)
#   PYTHON        Python with requests (default python3)
#   VRAM_LEND_BASE_URL  the server (live.py default http://127.0.0.1:8080)
set -u
MODE=${1:-}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}
MODEL=${MODEL:-swift15-flash-next-iq4xs-strata-2x3090-vramlend}
GPUS=${GPUS:-0000:01:00.0 0000:02:00.0}
RUNS=${RUNS:-3}
SEG=${SEGMENT_MIB:-512}
MATCH=${ENGINE_MATCH:-/build/strata( |$)}
OUT_DIR=${OUT_DIR:-$HERE/results/strata}
BASE=${VRAM_LEND_BASE_URL:-http://127.0.0.1:8080}
mkdir -p "$OUT_DIR"
COMMON=(--engine-match "$MATCH" --segment-mib "$SEG")

case "$MODE" in
  run1)
    big=$(printf 'big,%.0s' $(seq "$RUNS"))
    for g in $GPUS; do
      # the 8 GiB checks first, then the smaller ones; the over-lendable lend asks lendable + one segment
      "$PY" "$HERE/live.py" "$MODEL" "$OUT_DIR/run1-${g//:/-}.md" "${COMMON[@]}" --gpu "$g" \
        --checks "state,${big}each,full,over,two,busy" --over-extra 0 --two-mib 2048
    done
    # the server's own /unload stops the engine; the next request starts a new engine process under the same server
    set -- $GPUS
    "$PY" "$HERE/live.py" "$MODEL" "$OUT_DIR/run1-restart.md" "${COMMON[@]}" --gpu "$1" \
      --checks state,restart --restart-url '{base}/upstream/{model}/unload'
    ;;
  *)
    echo "usage: $0 run1" >&2
    exit 2
    ;;
esac
if [ "${UNLOAD:-1}" = 1 ]; then
  curl -s -X POST "$BASE/api/models/unload/$MODEL" >/dev/null
fi

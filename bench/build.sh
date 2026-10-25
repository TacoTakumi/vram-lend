#!/bin/bash
# Build a Maya tree with the VRAM Lend branch, the CUDA engines and (by default) the test suite.
#
#   build.sh <maya tree> [build dir] [cmake --build targets...]
#
# Environment (all optional):
#   CUDA_HOME    CUDA toolkit (default /usr/local/cuda)
#   CUDA_ARCHS   CMAKE_CUDA_ARCHITECTURES (default 86;89;120: RTX 30, 40 and 50 series)
#   GGML_DIR     llama.cpp tree for ggml (default <tree>/third_party/llama.cpp)
#   TESTS        ON or OFF: build the tests (default ON)
#   JOBS         parallel jobs (default: nproc)
#   MEM_CAP      a memory cap for the build, e.g. 24G (uses systemd-run --user --scope)
#   CMAKE, NINJA the tools (default: cmake and ninja on PATH)
set -u
T=${1:?usage: build.sh <maya tree> [build dir] [targets...]}; B=${2:-build-test}; shift; shift || true
CUDA_HOME=${CUDA_HOME:-/usr/local/cuda}
GGML_DIR=${GGML_DIR:-$T/third_party/llama.cpp}
CMAKE=${CMAKE:-cmake}; NINJA=${NINJA:-ninja}
export PATH=$CUDA_HOME/bin:$PATH
CAP=()
[ -n "${MEM_CAP:-}" ] && CAP=(systemd-run --user --scope -q -p MemoryMax="$MEM_CAP")
cd "$T" || exit 1
echo "=== configure $(date +%T)"
"${CAP[@]}" "$CMAKE" -G Ninja -DCMAKE_MAKE_PROGRAM="$(command -v "$NINJA")" -S . -B "$B" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS:-86;89;120}" \
  -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" -DCUDAToolkit_ROOT="$CUDA_HOME" \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_BUILD_TESTS="${TESTS:-ON}" \
  -DSTRATA_GGML_DIR="$GGML_DIR" -DSTRATA_PYTHON3="$(command -v python3)" > "$B-configure.log" 2>&1 \
  || { echo "configure FAIL"; tail -30 "$B-configure.log"; exit 1; }
echo "=== build $(date +%T)"
"${CAP[@]}" "$CMAKE" --build "$B" -j "${JOBS:-$(nproc)}" "$@" -- -k 0 > "$B-build.log" 2>&1; rc=$?
echo "build rc=$rc $(date +%T)"; grep -E "FAILED|error" "$B-build.log" | head -20
exit $rc

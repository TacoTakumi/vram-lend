#!/bin/bash
# The whole test suite on a Maya tree with the VRAM Lend branch: build with tests (build.sh), CTest on one GPU, then
# the server and tools unit tests on the CPU.
#
#   test-suite.sh <maya tree> <CUDA device index for CTest>
#
# Environment:
#   STRATA_TOKENIZER  required by the server tests: a GLM-5.3-Flash tokenizer directory
#   PYTHON            the tree's Python (default <tree>/.venv/bin/python)
#   CTEST             ctest (default: ctest on PATH)
#   plus the build.sh variables
set -u
T=${1:?usage: test-suite.sh <maya tree> <device index>}; C=${2:?device index}
: "${STRATA_TOKENIZER:?set STRATA_TOKENIZER to a GLM-5.3-Flash tokenizer directory}"
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-$T/.venv/bin/python}
"$HERE/build.sh" "$T" build-test || exit 1
cd "$T" || exit 1
echo "=== ctest device $C $(date +%T)"
CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=$C STRATA_TEST_DEV=0 \
  "${CTEST:-ctest}" --test-dir build-test --timeout 600 > build-test-ctest.log 2>&1
echo "ctest rc=$? $(date +%T)"
grep -E "tests passed|Failed|\*\*\*|Not Run|Timeout" build-test-ctest.log | head -40
echo "=== server tests"
CUDA_VISIBLE_DEVICES="" "$PY" -W ignore -m unittest discover -s serve -p 'test_*.py' -t . > serve-tests.log 2>&1
tail -3 serve-tests.log
echo "=== tools tests"
CUDA_VISIBLE_DEVICES="" "$PY" -m unittest $(ls tools/test_maya_*.py | sed 's#/#.#; s#\.py$##') 2>&1 | tail -2

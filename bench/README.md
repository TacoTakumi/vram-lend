# Bench scripts and results

The scripts that produced the results in [results/](results/), with every
path, URL and model id as a parameter. They were run against the Maya fork
and the Strata fork, each on its branch `vram-lend`, served through llama-swap. Another server works when it
serves the OpenAI chat API and the [/vram routes](../docs/api.md):
pass `--base-url` and `--vram-url` to the Python scripts.

| Script | What it does |
|---|---|
| `live.py` | The live checks: 8 GiB lend and return with a streaming request (memory, answer times, token gaps), a lend on each GPU, over lendable, two leases, a return while another process holds the memory, a lend during a prompt read, the prompt rate at full lendable, a late lend, decode cost, and an engine restart. Appends a markdown table to its output file. `live.py --help` lists the options |
| `vision.py` | One picture request while `/vram/state` is polled every 100 ms: the answer, the encoder's lease during the encode, and no lease after |
| `live-glm.sh` | The GLM-5.3-Flash run of `live.py` (five-GPU and three-GPU layouts) |
| `live-deepseek.sh` | The DeepSeek-V4.1 run of `live.py` |
| `live-strata.sh` | The Strata run of `live.py` on a two-GPU layer split (`run1`: lend, return, isolation, full lendable, edges, an engine restart; `run2`: the prompt rate at full lendable, a lend during a long prompt read, decode t/s and the engine's hit rate before, during and after an 8 GiB lend) |
| `soak-strata.sh` | A Strata soak: lend/return cycles of varied sizes on alternating GPUs, each while a counting request streams (answers, times, memory, token gaps, the numbers in order), the engine log's CUDA errors, non-finite warnings and restarts over the run, and an optional command (a quality suite) run while memory is lent on every GPU |
| `build.sh` | Builds a Maya tree with the CUDA engines and the tests |
| `test-suite.sh` | Builds with tests, runs CTest on one GPU, then the server and tools unit tests |

Each shell script documents its environment variables at the top. The live
checks need `nvidia-smi`, `pgrep`, Python 3 with `requests`, and a model that
the server can load. The busy-return check starts `live.py --hog <PCI address>`
as a second process that holds GPU memory, so it needs the CUDA runtime
(`CUDART`, default `/usr/local/cuda/lib64/libcudart.so`).

The live checks lend real memory from a serving engine and restart it. Run
them on a host where nothing else depends on that engine.

## Results

| File | Engine |
|---|---|
| [results/glm.md](results/glm.md) | GLM-5.3-Flash, five-GPU and three-GPU layouts |
| [results/deepseek.md](results/deepseek.md) | DeepSeek-V4.1, five-GPU layout |
| [results/vision.md](results/vision.md) | GLM-5.3-Flash vision encoder as a borrower |
| [results/strata.md](results/strata.md) | Strata, Swift 1.5 IQ4_XS on a 2x RTX 3090 layer split |

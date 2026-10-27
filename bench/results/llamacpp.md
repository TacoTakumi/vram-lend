# llama.cpp: VRAM Lend live checks

Host: 2x RTX 3090 (0000:01:00.0, 0000:02:00.0), RTX 5060 Ti (0000:05:00.0), 2x RTX 4060 Ti (0000:09:00.0, 0000:0a:00.0), CUDA 13.3. Engine: llama.cpp (llama-server), branch [vram-lend](https://github.com/TacoTakumi/llama.cpp/tree/vram-lend) on a fork of [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp): master 961c376a2 (2026-10-26), the per-device `--moe-cache-mib` of PR [#30205](https://github.com/ggml-org/llama.cpp/pull/30205) and ten VRAM Lend commits. Model: Swift 1.5 (Qwen3.8 Flash-Next) IQ4_XS with the MTP draft head and the vision encoder, served through llama-swap (entry `swift15-flash-next-iq4xs-mtp-vision-vramlend`). Layout: an explicit tensor split over the five GPUs (`--fit off -ts 28,30,14,15,13`); the experts of the 14 layers on the RTX 5060 Ti and the first RTX 4060 Ti stay in host RAM behind an 8192 MiB MoE expert cache on each of those two cards; `--vram-lend-max-mib 0,0,8192,8192,0`. Lendable: 8112 MiB on each of 0000:05:00.0 and 0000:09:00.0. The RTX 3090s and the second RTX 4060 Ti hold whole layers and lend nothing.

In llama.cpp a lend frees the device's cache buffer and allocates it again with fewer slots (whole slots of 2.54 MiB on this model), rebuilding the cache lists and slot maps of that device only; a return allocates the pre-lend slot count again. Lendable is the cache minus a floor of 30 slots per expert group (one decode step: 10 experts used x 3 tokens), in whole slots. There is no VMM pool.

The Build column gives the commit of the fork branch a row comes from. `0c61d2dcc` is the branch after its eight lend commits; `a4d6eeef7` adds the two fixes of the code review (a return never gives back more than its lend took, and a mutex over the three lend entry points). The fixes do not touch the decode or prompt paths, so the cost and gate rows were not run again. Memory figures are the engine process as `nvidia-smi --query-compute-apps` shows it, polled every 50 ms. Prompt and decode rates are the server's own timings in the chat answers. Measured 2026-10-26 and 2026-10-27.

Scripts: `live.py` (the state, lend, return, isolation, edges and restart checks, and the late lend). The prompt rate, the cost table and the quality gate ran through short scripts around the same calls; those scripts are not in this repo.

## Summary

| Check | Result | Build | Detail |
|---|---|---|---|
| 8 GiB lend frees the memory | Passed | 0c61d2dcc, a4d6eeef7 | 0000:05:00.0, the full lendable 8112 MiB asked, lease 8112 MiB. Lend answer 0.04 to 0.05 s. The engine process drops by 8112 MiB, shown by the driver at or before the answer |
| Decode stall across the lend | Passed | 0c61d2dcc, a4d6eeef7 | Largest token gap 0.11 s (target at most 1 s). The 1500-token stream finished normally at 49.4 t/s, and a chat request after the return answers |
| Return maps it back | Passed | 0c61d2dcc, a4d6eeef7 | Answer 0.03 to 0.10 s. Largest token gap 0.11 to 0.12 s. Engine process back at its size from before the lend, 0 MiB off, 0.01 to 0.08 s after the send |
| Any GPU, others unchanged | Passed | 0c61d2dcc | 2 GiB lend and return on each lending card: lease 2049 MiB (whole slots), this card down 2050 MiB, every other GPU changed by 0 MiB. The three GPUs with no cache answer 409 `{"max_mib": 0}` |
| Lendable follows the rule | Passed | 0c61d2dcc | 8192 MiB cache minus 30 slots of 2.54 MiB, in whole slots: 8112 MiB on both cards. A lend of 8192 answers 409 `{"max_mib": 8112}` |
| Full lendable keeps the prompt rate | Passed | 0c61d2dcc | 50035-token cold prompt on 0000:05:00.0: 645 t/s with 8112 MiB lent against 637 t/s with nothing lent (1.012). 63k-token cold prompt with both cards fully lent: 525 t/s against 516 t/s (1.017). The prompt path reads the host experts directly, so a lend costs nothing there |
| Lendable before the lend = after the return | Passed | 0c61d2dcc | 10 cycles of 2048 MiB and 10 of the full lendable on 0000:05:00.0: lendable 8112 MiB after every return. 100 reads of `/vram/state` interleaved with cycles: 0 mismatches between the lease sum and lent |
| Lend during a prompt read | Passed | 0c61d2dcc | 63k-token prompt, a 2 GiB lend sent 2 to 3 s into the read: answered 200 after 1.18 to 1.4 s, at the end of the 512-token batch in flight. The read finished at 511 t/s and the lease was returned after it |
| Late lend (503, the server gives the lease back) | Passed | 0c61d2dcc | With the server's wait set to 1 ms (`--vram-lend-wait-ms 1`, default 30000) the same lend answered 503 after 0.0 s, and the state showed no lease and 0 lent at the first poll: the queued lend was dropped with the request. At the default wait the late case does not occur on this engine, because a lend is answered at the next batch boundary. The server's give-back path is covered by its unit tests (`tools/server/tests/unit/test_vram_lend.py`, 26 tests) |
| Two leases on one GPU | Passed | 0c61d2dcc, a4d6eeef7 | 0000:05:00.0: lend A 2 GiB, lend B 2 GiB, return A, return B. Lent 2049 / 4098 / 2049 / 0 MiB with the matching leases listed, engine process down 2050 / 4098 / 2050 / 0 MiB, a short stream after each step, lendable 8112 MiB at the start and the end |
| Over lendable | Passed | 0c61d2dcc, a4d6eeef7 | 9139 MiB on 0000:05:00.0: 409 `{"max_mib": 8112}`. Engine memory unchanged |
| Return while the memory is in use | Passed | 0c61d2dcc | 409 with `retry` while a second process held 6961 MiB of the card, lease still listed. 200 after it freed the memory |
| Engine restart | Passed | 0c61d2dcc | The server's own unload stopped the engine with a 1025 MiB lease open, the next request started a new engine in 15 s. No leases, lent 0 on every GPU, the old id answers 404, and ids start again at 1 |
| GPU address forms | Passed | 0c61d2dcc | `0000:05:00.0`, `00000000:05:00.0` and `05:00.0` each lend and answer with `0000:05:00.0` |
| Decode cost of 8 GiB lent | Passed (measured and reported) | 0c61d2dcc | 300-token runs, 3 per phase: 58.3 t/s with nothing lent, 25.0 t/s with 8112 MiB lent on 0000:05:00.0 (-57%), 57.6 t/s on the first run after the return (within 5% after 6.4 s). Table [below](#decode-and-prompt-cost) |
| Quality with 8 GiB lent on each lending card | Passed | 0c61d2dcc | Coding 16/16 and math 12/12 with nothing lent. Needles at 32k and 128k, depths 10/50/90: 6/6 with nothing lent and 6/6 with 8112 MiB lent on both cards (320 to 325 t/s prompt). 0 CUDA errors in the log |
| Engine test under the memory checker | Passed | 0c61d2dcc | `tests/test-vram-lend`, Qwen3.6-35B-A3B on one RTX 3090 with 40 layers of experts on host and a 4096 MiB cache: a 1024 MiB lend moves the device's free memory by 1024 MiB and a return moves it back. Three cycles of decode, lend, decode, a 511-token prompt, return, decode under `compute-sanitizer --tool memcheck` with CUDA graphs on: 0 errors, every return restores the slot count, identical greedy text in all nine generation phases |
| Unit and server tests | Passed | a4d6eeef7 | `test_vram_lend.py` 26 passed on the stories15M MoE preset (lend, return, 404, 409, 501, 503 and the self-return of a late lease). Whole server suite 459 passed, 0 failed |
| HIP and SYCL builds | Not run | - | No toolchain on the host. The lend code lives in the MoE cache and the server; the cache itself is CUDA only in this build |

## Decode and prompt cost

Same host, 300-token greedy decode (mean of 3 runs) and one cold 63k-token prompt. The daily entry is the same model on the same five GPUs under llama.cpp's `--fit` split, with no expert cache: every expert resident. The twin is the lend-ready layout above with two cache sizes.

| Entry | Cache per lending card | Decode, nothing lent | 63k prompt, nothing lent | Lease on 0000:05:00.0 | Decode while lent | Prompt while lent | First decode run after the return |
|---|---|---|---|---|---|---|---|
| Daily entry, `--fit` split | none | 58.6 t/s | 827 t/s | - | - | - | - |
| Lend-ready layout | 8192 MiB | 58.3 t/s | 527 t/s | 8112 MiB | 25.0 t/s | 530 t/s | 57.6 t/s, within 5% after 6.4 s |
| Lend-ready layout | 6784 MiB | 60.4 t/s | 534 t/s | 6703 MiB | 25.3 t/s | 533 t/s | 58.1 t/s, within 5% after 6.4 s |

The lend-ready layout costs nothing on decode against the `--fit` split: the caches serve the host experts as well as resident experts do. It costs 36% on prompt reading, because the prompt path reads the 18 GiB of host experts over PCIe on every prompt; a lend changes nothing there. A full-lendable lease leaves each expert group its 30-slot floor, so decode while lent runs at the host-expert rate, 25 t/s, whatever the cache size. The 8192 MiB cache keeps 1.4 GiB more lendable than 6784 MiB at the same cost, so the served entry uses 8192 MiB. Smaller lends cost less on decode; this was not measured.

`--fit` cannot build the lend-ready layout: its host-expert layers always land on the last device, so the split is explicit.

## Run on 0000:05:00.0, 8 GiB and the edges

Build a4d6eeef7, 2026-10-27, after the review fixes. Token gaps rounded to 3 places, decode rates to 1.

| check | result | detail |
|---|---|---|
| state | PASS | /vram/state gpus ['0000:01:00.0', '0000:02:00.0', '0000:05:00.0', '0000:09:00.0', '0000:0a:00.0'], engine GPUs from nvidia-smi the same |
| two-lend-A | PASS | lend A: 200 {'id': '1', 'gpu': '0000:05:00.0', 'mib': 2049} (0.058 s); state lent 2049 leases ['1']; engine MiB on 0000:05:00.0 8566 (down 2050, block 3); request after it: finish length err None, 64 deltas |
| two-lend-B | PASS | lend B: 200 {'id': '2', 'gpu': '0000:05:00.0', 'mib': 2049} (0.023 s); state lent 4098 leases ['1', '2']; engine MiB on 0000:05:00.0 6518 (down 4098, block 3); request after it: finish length err None, 64 deltas |
| two-return-A | PASS | return A: 200 {'id': '1', 'gpu': '0000:05:00.0', 'mib': 2049} (0.029 s); state lent 2049 leases ['2']; engine MiB on 0000:05:00.0 8566 (down 2050, block 3); request after it: finish length err None, 64 deltas |
| two-return-B | PASS | return B: 200 {'id': '2', 'gpu': '0000:05:00.0', 'mib': 2049} (0.041 s); state lent 0 leases []; engine MiB on 0000:05:00.0 10616 (down 0, block 3); request after it: finish length err None, 64 deltas |
| two-leases | PASS | ids ['1', '2']; lendable 8112 at the start, 8112 at the end, lent 0 |
| lend-8g-size | PASS | asked 8112, lease 8112 MiB |
| lend-8g-free | PASS | engine process on 0000:05:00.0 down 8112 MiB; driver showed >= 8112 MiB freed 0.07 s after send, 0.02 s after the answer (lend call 0.05 s) |
| lend-8g-stall | PASS | largest token gap across the lend 0.108 |
| return-8g | PASS | return 200 in 0.03 s; back to 10616 MiB (before 10616), driver showed it 0.01 s after send |
| return-8g-stall | PASS | largest token gap across the return 0.115 |
| return-8g-state | PASS | state before {'gpu': '0000:05:00.0', 'lendable': 8112, 'lent': 0} after {'gpu': '0000:05:00.0', 'lendable': 8112, 'lent': 0} |
| lend-8g-request | PASS | request finish length error None, 1500 deltas, decode 49.3 /s |
| after-return-chat | PASS | a chat request after the return answers |
| over-lendable | PASS | lend of 9139 over 8112: 409 {'max_mib': 8112}; engine MiB 10616 -> 10616 |

The block size `live.py` reports (3 MiB) is the cache slot rounded up: a lease is a whole number of 2.54 MiB slots, so a 2048 MiB request gives a 2049 MiB lease and the process drops by 2050 MiB.

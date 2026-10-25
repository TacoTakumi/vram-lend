# Strata: VRAM Lend live checks

Host: 2x RTX 3090 (0000:01:00.0, 0000:02:00.0), RTX 5060 Ti (0000:05:00.0), 2x RTX 4060 Ti (0000:09:00.0, 0000:0a:00.0), CUDA 13.3. Engine: Strata, branch `vram-lend` on a fork of [Niko1221/Strata](https://github.com/Niko1221/Strata): upstream v0.2.6 with the per-GPU segmented expert caches of PR [#2522](https://github.com/Niko1221/Strata/pull/2522), fixes for that PR and the VRAM Lend commits. Model: Swift 1.5 (Qwen3.8 Flash-Next) IQ4_XS on a layer split over the two RTX 3090, with the vision encoder on, served through llama-swap (entry `swift15-flash-next-iq4xs-strata-2x3090-vramlend`, `"vram_lend": true`). The engine probes the PCIe link of each card at load: 0000:01:00.0 (CUDA0) 24.4 GB/s, 0000:02:00.0 (CUDA1) 6.1 GB/s. Segments are 512 MiB. Lendable: 12800 MiB (25 segments) on 0000:01:00.0, 12288 MiB (24 segments) on 0000:02:00.0.

The Build column gives the commit of the fork branch a row comes from. `7b34e3c9` is the branch with its last two engine commits of the live runs: 60d05fe0 (a return maps a lease's segments all or none and refills its experts in the background) and 7b34e3c9 (a lend that comes during a prompt read is answered at the next prompt chunk). Every live run with VRAM Lend on used it. `bd4233b1` is the engine binary linked while the branch was at bd4233b1, before 60d05fe0: the option-off rows come from it, and 60d05fe0 and 7b34e3c9 change only code that runs with VRAM Lend on. `4a4783b5` adds the refill with no request running (section below). `v0.2.6` is upstream Strata v0.2.6 (4b9e05a2). Memory figures are the engine process as `nvidia-smi --query-compute-apps` shows it, polled every 50 ms. Prompt and decode rates and hit rates are the engine's own log lines. Measured 2026-10-25.

Scripts: `live-strata.sh` (`run1`, `run2`, both `live.py`) and `soak-strata.sh`.

## Summary

| Check | Result | Build | Detail |
|---|---|---|---|
| 8 GiB lend frees the memory | Passed | 7b34e3c9 | Each RTX 3090, 3 runs: 8192 MiB asked, lease 8192 MiB (16 segments). Lend answer 0.04 to 0.05 s. The engine process drops by exactly 8192 MiB, shown by the driver 0.01 to 0.04 s after the answer |
| Decode stall across the lend | Passed | 7b34e3c9 | Largest token gap 0.046 to 0.056 s (target at most 1 s). All 6 streams finished normally, 1486 to 1491 tokens at 105.7 to 117.1 t/s, and a chat request after each return answers |
| Return maps it back | Passed | 7b34e3c9 | Answer 0.01 to 0.03 s. Largest token gap 0.029 to 0.039 s on 0000:01:00.0, 0.067 to 0.074 s on 0000:02:00.0. Engine process back at its size from before the lend, 0 MiB off, 0.02 to 0.06 s after the send |
| Any GPU, others unchanged | Passed | 7b34e3c9 | 2 GiB lend and return on each RTX 3090, two runs each: the engine on the other RTX 3090 and the three GPUs the engine does not use changed by 0 MiB |
| Lendable follows the rule | Passed | 7b34e3c9 | Pool minus the prompt path's borrow minus the floor, from the engine's load lines: 17834 - 4484 - 325 = 13025 MiB on 0000:01:00.0 (lendable 12800), 17342 - 4484 - 325 = 12533 MiB on 0000:02:00.0 (12288). Both within one segment |
| Full lendable keeps the prompt rate, 0000:01:00.0 | Passed | 7b34e3c9 | 24035-token cold prompt, 4 cycles: 1606 to 1607 t/s with 12800 MiB lent against 1612 to 1630 t/s with nothing lent, 0.985 to 0.997 (target 0.90 or more). No fallback to one token at a time |
| Full lendable keeps the prompt rate, 0000:02:00.0 | Below the 90% target, accepted | 7b34e3c9 | 3 valid cycles: 1130 to 1143 t/s with 12288 MiB lent against 1556 to 1653 t/s, 0.690 to 0.734. The read takes 21.0 to 21.3 s instead of 14.5 to 14.7 s. Cause: the lent experts are uploaded again for each prompt chunk over the 6.1 GB/s link, see [below](#prompt-rate-at-full-lendable). Accepted as the measured cost of the slow link; a cap on lendable for slow-link cards is future work |
| Lendable before the lend = after the return | Passed | 7b34e3c9 | 8 GiB lends: 12800 = 12800 and 12288 = 12288 MiB, 3 runs each, a 1500-token stream across each lend and return. Full lendable lend with a 400-token stream while lent (86 t/s): 12800 = 12800 and 12288 = 12288 MiB |
| Lend during a prompt read | Passed | 7b34e3c9 | 80032-token prompt, a 2048 MiB lend sent 2.0 s into the request: answered 200 after 7.7 and 10.9 s (two runs), at token 16384, the next prompt-chunk boundary the first card reached. One 8192-token chunk takes 5.9 and 7.6 s, so the answer came 1.3 to 1.45 chunk times after the lend was sent (1.65 in an earlier run on this layout): on the layer split the second card's drain adds the time over one chunk. The request finished normally and the lease was returned after it |
| Late lend (503, the server gives the lease back) | Not reachable | 7b34e3c9 | The engine answers a lend at a prompt-chunk boundary, inside the server's 30 s wait, so a lend during the 80K-token read did not wait 30 s. The server's give-back path is covered by its unit tests (`serve/test_vram_lend.py`, classes `LateAnswers` and `LateLeaseStillInUse`, 4 tests OK) |
| Two leases on one GPU | Passed | 7b34e3c9 | Each RTX 3090: lend A 2 GiB, lend B 2 GiB, return A, return B. Lent 2048 / 4096 / 2048 / 0 MiB with the matching leases listed, engine process down 2048 / 4096 / 2048 / 0 MiB, a short stream after each step, lendable the same at the end |
| Over lendable | Passed | 7b34e3c9 | 13312 MiB on 0000:01:00.0: 409 {"max_mib": 12800}. 12800 MiB on 0000:02:00.0: 409 {"max_mib": 12288}. Engine memory unchanged |
| Return while the memory is in use | Passed | 7b34e3c9 | Each RTX 3090: 409 with retry while a second process held the memory, lease still listed. 200 after it freed the memory |
| Engine restart | Passed | 7b34e3c9 | The server's own unload stopped the engine with a 1024 MiB lease open, the next request started a new engine in 34 s. No leases, lent 0 on both GPUs, the old id answers 404, and the next lease got a new id |
| Decode cost of 8 GiB lent | measured | 7b34e3c9 | 1000-token runs, 3 per phase, two runs of the check: -5.6 and -6.5% on 0000:01:00.0, -11.5 and -9.5% on 0000:02:00.0. Hit rate 99.4-99.8% before, 98.1-98.6% and 96.9-97.6% lent. Table [below](#decode-before-during-and-after-an-8-gib-lend) |
| Return on the slow link, hit rate after it | Passed | 7b34e3c9 | A return sent 30 tokens into a 1000-token stream: largest token gap 0.040 s on 0000:01:00.0, 0.084 to 0.086 s on 0000:02:00.0. Hit rate back within 1 point of its value before the lend by about 955 decoded tokens after the return |
| Soak, 50 cycles | Passed | 7b34e3c9 | 50 lend/return cycles of 2048 to 8192 MiB in 512 MiB steps, alternating GPUs, each during a temperature-0 "count from 1 to 300" stream. Every lend 200 (0.011 to 0.068 s), every return 200 (0.003 to 0.029 s), no 409. Memory down by exactly the lease on every lend and back on every return. Largest token gap 0.077 s. Every answer counts 1 to 300 in order. Engine log: no CUDA error, no non-finite warning, no restart |
| Quality with 8 GiB lent on each RTX 3090 | Passed | 7b34e3c9 | Coding 16/16, math 12/12, in 178 s against 110 s with nothing lent (the hit rate climbs from 81% to 97% during the run). After the returns, same load: coding 15/16, math 12/12, within the known one-task spread of the baseline |
| Serial decode loop | Passed | 7b34e3c9 | With VRAM Lend on, `--pipeline-windows 2` and `--batch 2` each log one line saying VRAM Lend runs the serial decode loop, and requests are served. With VRAM Lend off both options log the same lines and give the same answers as v0.2.6 |
| VRAM Lend off | Passed | bd4233b1 | Two fresh loads: POST /vram/lend and /vram/return answer 501, GET /vram/state lists both RTX 3090 at 0/0 with no leases, no lend lines in the engine log, the bench rows within 0.981 to 1.030 of v0.2.6 (table [below](#vram-lend-off-against-v026)) |
| Base: Strata v0.2.6 on this layout | Passed | v0.2.6 | Before any lend code: coding 16/16, math 12/12, needles 6/6 at 32k and 128k, the vision test image read correctly, the bench rows 1.003 to 1.205 of the older served v0.1.44.1 build |
| HIP build | Not run | - | No HIP toolchain on the host. In the source, the new engine code sits behind the same no-VMM guard as Strata's elastic VRAM code, and a build or GPU without CUDA virtual memory answers every GPU with lendable 0 (unit test on the CUDA build) |
| SYCL build | Not run | - | No SYCL toolchain on the host. The SYCL engine does not take the VRAM Lend flag: the server starts it without the flag, says once that VRAM Lend is not available there, and the /vram routes answer as for an engine that cannot lend (server unit tests) |

The engine logs each lend. An 8192 MiB lend on 0000:01:00.0 always took the same 16 coldest segments by the expert profile's rank, and 2 GiB lends picked different segments by heat; lendable did not move with that choice.

## Prompt rate at full lendable

24035-token prompts, each a new random word sequence, 0 tokens reused. Engine t/s, the second run of the check:

| GPU | Cycle | Nothing lent | Full lendable lent | Right after the return | After one 1000-token decode request |
|---|---|---|---|---|---|
| 0000:01:00.0 | 1 | 1628 | 1607 | 1608 | 1646 |
| 0000:01:00.0 | 2 | 1627 | 1607 | 1607 | 1646 |
| 0000:02:00.0 | 1 | 1653 | 1140 | 1142 | 1567 |
| 0000:02:00.0 | 2 | 1556 | 1143 | 1145 | 1540 |

The lendable rule keeps the prompt path's borrowed slots and the floor, so the prompt path never falls back to one token at a time. But each 8192-token chunk routes nearly every expert of the card's layers, so the experts in the lent segments are uploaded again for each chunk. On 0000:02:00.0 the extra read time is 6.5 s over 3 chunks, 2.2 s per chunk; 12288 MiB over the 6.1 GB/s link takes 2.1 s. On 0000:01:00.0 the same upload is 0.55 s per chunk at 24.4 GB/s and is hidden by compute (0.2 s extra in all). So the full lendable keeps the prompt rate on a fast link only. This is accepted as the measured cost of the 6.1 GB/s link; a cap on lendable for slow-link cards is future work. By the same arithmetic, about 3 GiB lent on 0000:02:00.0 would stay within 10% (an estimate, not measured).

## Refill after a return

In the build `7b34e3c9` the returned slots were filled again only at the ends of decode windows. While a request decodes this is quick: an 8 GiB return refilled 2495 to 2885 of 2677 to 3227 owed slots in 0.48 to 0.59 s on 0000:01:00.0 and 1.42 to 1.52 s on 0000:02:00.0. With no request running after a return, the refill waited for the next decode windows, so prompt reads right after the return stayed at the lent rate (1142 and 1145 t/s on 0000:02:00.0), and each refill left about one slot in eight empty.

The build `4a4783b5` also refills while the engine is idle (between 1 ms waits for input, so a new request does not wait) and between the chunks of a prompt read, into band slots below the prompt's loan only, and fills every owed slot. Measured on it, one engine start, cold 24k-token prompts:

| GPU | Lent | Fresh t/s | Lent t/s | After a return with no request running | Ratio | Refill done after the return | Slots filled |
|---|---|---|---|---|---|---|---|
| 0000:02:00.0 | 12288 MiB | 1641.0 | 1138.2 | 1633.8 | 0.996 | 2.2 s | 4841 of 4841 |
| 0000:02:00.0 | 12288 MiB | 1634.7 | 1140.3 | 1633.9 | 1.000 | 2.2 s | 4841 of 4841 |
| 0000:01:00.0 | 12800 MiB | 1633.3 | 1611.8 | 1631.1 | 0.999 | 0.67 s | 5042 of 5042 |
| 0000:01:00.0 | 12800 MiB | 1639.9 | 1613.7 | 1638.8 | 0.999 | 0.66 s | 5042 of 5042 |

A return followed at once by a 48035-token prompt on 0000:02:00.0: the read stopped at four chunk boundaries to land the refill (216 to 259 ms each) and the refill was done during the read. A short chat right after a 12 GiB return had its first token after 0.728 s, against 0.726 s with nothing owed.

A GET /vram/state sent during a long prompt read is also answered at the next prompt-chunk boundary (5.7 and 7.3 s for the first poll after the lend in the late-lend check).

## Decode before, during and after an 8 GiB lend

Same prompt, 1000 tokens per run, engine t/s (mean) and decode hit rate. Before: nothing lent. Lent: 8192 MiB lent on that GPU. Return run: the run during which the lease was returned, 30 tokens in. After: the next 3 runs. A and B are two runs of the check.

| GPU | Run | Before t/s | Before hit % | Lent t/s | Lent hit % | Return run t/s / hit % | After t/s | After hit % |
|---|---|---|---|---|---|---|---|---|
| 0000:01:00.0 | A | 118.2 122.4 123.1 (121.2) | 99.6 99.7 99.8 | 112.8 117.8 112.7 (114.4) | 98.1 98.6 98.4 | 119.7 / 99.5 | 119.5 123.7 116.8 (120.0) | 99.7 99.8 99.7 |
| 0000:01:00.0 | B | 117.5 119.2 120.3 (119.0) | 99.5 99.6 99.6 | 112.4 109.5 111.8 (111.2) | 98.1 98.1 98.3 | 117.1 / 99.3 | 118.4 115.1 119.1 (117.5) | 99.5 99.5 99.6 |
| 0000:02:00.0 | A | 119.1 120.4 120.0 (119.8) | 99.6 99.7 99.7 | 104.9 106.6 106.8 (106.1) | 96.9 97.3 97.6 | 106.8 / 99.2 | 121.3 121.5 123.5 (122.1) | 99.6 99.6 99.6 |
| 0000:02:00.0 | B | 115.7 120.0 119.0 (118.2) | 99.7 99.4 99.6 | 109.6 104.9 106.6 (107.0) | 97.4 96.9 97.4 | 109.3 / 99.0 | 116.5 116.8 118.0 (117.1) | 99.4 99.3 99.4 |

Lendable before and after each check equal (12800 / 12288 MiB), lent 0 after, every request finished without error. The client's stream rates are within 1 t/s of the engine's figures.

## VRAM Lend off against v0.2.6

No `"vram_lend"` key and no engine flag, two fresh loads each, vision on. Decode rows are the mean of 4 runs, prefill rows of 2.

| Row | v0.2.6 t/s | VRAM Lend build, off, t/s | Off / v0.2.6 |
|---|---|---|---|
| decode short | 135.0 | 136.8 | 1.013 |
| decode 24k | 151.6 | 148.7 | 0.981 |
| decode 74k | 134.0 | 138.0 | 1.030 |
| prefill 24k | 1585.5 | 1582.1 | 0.998 |
| prefill 57-74k | 1699.7 | 1699.7 | 1.000 |

Greedy text differs between two fresh loads of the same build on v0.2.6 too, so it is not compared.

## Run on 0000:02:00.0, first 8 GiB run and the edges

Token gaps rounded to 3 places, decode rates to 1.

| check | result | detail |
|---|---|---|
| state | PASS | /vram/state gpus ['0000:01:00.0', '0000:02:00.0'], engine GPUs from nvidia-smi ['0000:01:00.0', '0000:02:00.0'] |
| lend-8g-size | PASS | asked 8192, lease 8192 MiB |
| lend-8g-free | PASS | engine process on 0000:02:00.0 down 8192 MiB; driver showed >= 8192 MiB freed 0.06 s after send, 0.02 s after the answer (lend call 0.04 s) |
| lend-8g-stall | PASS | largest token gap across the lend 0.054 |
| return-8g | PASS | return 200 in 0.03 s; back to 23482 MiB (before 23482), driver showed it 0.06 s after send |
| return-8g-stall | PASS | largest token gap across the return 0.071 |
| return-8g-state | PASS | state before {'gpu': '0000:02:00.0', 'lendable': 12288, 'lent': 0} after {'gpu': '0000:02:00.0', 'lendable': 12288, 'lent': 0} |
| lend-8g-request | PASS | request finish length error None, 1490 deltas, decode 105.7 /s |
| after-return-chat | PASS | a chat request after the return answers |
| each-0000:01:00.0 | PASS | lend 200 {'id': '13', 'gpu': '0000:01:00.0', 'mib': 2048} (0.01 s), this card down 2048 MiB, others {'0000:02:00.0': 0}, GPUs without the engine (whole GPU) {'0000:05:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, block 512 MiB, return 200, back to 22278 (before 22278) |
| each-0000:02:00.0 | PASS | lend 200 {'id': '14', 'gpu': '0000:02:00.0', 'mib': 2048} (0.01 s), this card down 2048 MiB, others {'0000:01:00.0': 0}, GPUs without the engine (whole GPU) {'0000:05:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, block 512 MiB, return 200, back to 23482 (before 23482) |
| full-lendable | PASS | lend of the full lendable 12288: 200 {'id': '15', 'gpu': '0000:02:00.0', 'mib': 12288} (0.07 s); request while lent: finish length err None, 396 deltas, decode 86.1 /s; return 200; chat after True; state before {'gpu': '0000:02:00.0', 'lendable': 12288, 'lent': 0} after {'gpu': '0000:02:00.0', 'lendable': 12288, 'lent': 0} |
| over-lendable | PASS | lend of 12800 over 12288: 409 {'max_mib': 12288}; engine MiB 23482 -> 23482 |
| two-lend-A | PASS | lend A: 200 {'id': '16', 'gpu': '0000:02:00.0', 'mib': 2048} (0.016 s); state lent 2048 leases ['16'], open 2048 MiB; engine MiB on 0000:02:00.0 21434 (down 2048, block 512); request after it: finish length err None, 63 deltas |
| two-lend-B | PASS | lend B: 200 {'id': '17', 'gpu': '0000:02:00.0', 'mib': 2048} (0.031 s); state lent 4096 leases ['16', '17'], open 4096 MiB; engine MiB on 0000:02:00.0 19386 (down 4096, block 512); request after it: finish length err None, 63 deltas |
| two-return-A | PASS | return A: 200 {'id': '16', 'gpu': '0000:02:00.0', 'mib': 2048} (0.010 s); state lent 2048 leases ['17'], open 2048 MiB; engine MiB on 0000:02:00.0 21434 (down 2048, block 512); request after it: finish length err None, 62 deltas |
| two-return-B | PASS | return B: 200 {'id': '17', 'gpu': '0000:02:00.0', 'mib': 2048} (0.005 s); state lent 0 leases [], open 0 MiB; engine MiB on 0000:02:00.0 23482 (down 0, block 512); request after it: finish length err None, 62 deltas |
| two-leases | PASS | ids ['16', '17']; lendable 12288 at the start, 12288 at the end, lent 0 |
| busy-return | PASS | hog: dev 1 held 1809 MiB (err 0, free was 2065); return while held 409 {'error': "lease 18's memory is still in use; it stays lent - try again later", 'retry': True}, lease listed True; after free 200 {'id': '18', 'gpu': '0000:02:00.0', 'mib': 2048} |
| restart-clears | PASS | lend 200 {'id': '19', 'gpu': '0000:01:00.0', 'mib': 1024}; engine stop 200 {"status": "unloaded"}; reload 200 in 34 s; state after {"gpus": [{"gpu": "0000:01:00.0", "lendable": 12800, "lent": 0}, {"gpu": "0000:02:00.0", "lendable": 12288, "lent": 0}], "leases": []}; return of the old id 404 {'error': 'no open lease 19'}; a new lend 200 {'id': '20', 'gpu': '0000:01:00.0', 'mib': 1024} |

The busy-return check holds the GPU's free memory minus 256 MiB (1809 MiB), a little less than the 2048 MiB lease. What is left free is less than one segment, so no segment of the lease can be mapped back, which is the case the check needs.

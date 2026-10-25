# DeepSeek-V4.1: VRAM Lend live checks

Host: 2x RTX 3090 (0000:01:00.0, 0000:02:00.0), RTX 5060 Ti (0000:05:00.0), 2x RTX 4060 Ti (0000:09:00.0, 0000:0a:00.0), CUDA 13.3. Engine: the Maya fork, branch `vram-lend`, rebased on upstream v1.1.27, served through llama-swap. Memory figures are the engine process as `nvidia-smi --query-compute-apps` shows it, polled every 50 ms. Measured 2026-10-24.

Layout: five GPUs. Script: `live-deepseek.sh` (`live.py`).

## Summary

| Check | Result | Detail |
|---|---|---|
| 8 GiB lend frees the memory | PASS | 0000:09:00.0 (RTX 4060 Ti): 8192 MiB asked, lease 8246 MiB. Lend answer 0.16 to 0.22 s, driver shows it free 0.01 to 0.05 s after the answer |
| Return maps it back | PASS | Answer 0.08 to 0.13 s. Lendable 10478 MiB before the lend and after the return |
| Decode stall | PASS | Largest token gap 0.23 to 0.28 s across the lend, 0.12 to 0.21 s across the return (target at most 1 s) |
| Any GPU, others unchanged | PASS | 2 GiB lend and return on each of the 5 GPUs. Other GPUs: 0 MiB change |
| Lend while requests run | PASS | A 4096 MiB lend sent during a 12K-token prompt read was answered after 13.8 s, at the end of the read. Return 0.35 s. Both requests finished normally |
| Late lend | PASS | A lend sent during an 80K-token prompt read answered 503 after 30.0 s. The server gave the late lease back by itself: no leases and nothing lent after, and the request finished normally |
| Full lendable keeps the prompt rate | PASS | 10292 MiB lent on 0000:09:00.0: a 4059-token prompt read at 337 t/s against 342 t/s with nothing lent (98%; 85% in an earlier run). No fallback to one token at a time |
| Over lendable | PASS | 409 {"max_mib": N} with N the GPU's lendable, engine memory unchanged |
| Two leases on one GPU | PASS | Returning the first leaves the second listed, lent equals the second lease |
| Return while the memory is in use | PASS | 409 with retry while a second process held the memory, lease still listed. 200 after it freed the memory |
| Engine restart | PASS | Reload in 49 s, no leases, lent 0 on every GPU |
| RAM copy mode | PASS | With 29.6 GB of experts copied into RAM and the engine's swap checks on: state, 8 GiB lend and return (lendable 10478 before and after), two leases and a 2 GiB lend on every GPU all pass, with no swap mismatch in the engine log |
| Decode cost of 8 GiB lent | no penalty visible | Decode rose through every run because the expert cache was still warming, so the cost of the lend is inside that trend. With 4 warm-up requests: 21.98 / 24.03 / 29.58 t/s before, lent, after |

Two defects were found and fixed before the rows above: lendable moved after a return (the floor followed heat, and later a moving swap spare), and in RAM copy mode each fill after a return leaked a copy slot. See [../../docs/implementing.md](../../docs/implementing.md#lessons-from-the-defects-found).

## Final run, five GPUs

| check | result | detail |
|---|---|---|
| state | PASS | /vram/state gpus ['0000:01:00.0', '0000:02:00.0', '0000:05:00.0', '0000:09:00.0', '0000:0a:00.0'], engine GPUs from nvidia-smi ['0000:01:00.0', '0000:02:00.0', '0000:05:00.0', '0000:09:00.0', '0000:0a:00.0'] |
| lend-8g-size | PASS | asked 8192, lease 8246 MiB |
| lend-8g-free | PASS | engine process on 0000:09:00.0 down 8246 MiB; driver showed >= 8192 MiB freed 0.17 s after send, 0.01 s after the answer (lend call 0.16 s) |
| lend-8g-stall | PASS | largest token gap across the lend 0.25 |
| return-8g | PASS | return 200 in 0.13 s; back to 15342 MiB (before 15342), driver showed it 0.14 s after send |
| return-8g-stall | PASS | largest token gap across the return 0.14 |
| return-8g-state | PASS | state before {'gpu': '0000:09:00.0', 'lendable': 10478, 'lent': 0} after {'gpu': '0000:09:00.0', 'lendable': 10478, 'lent': 0} |
| lend-8g-request | PASS | request finish length error None, 1500 deltas, decode 17.30 /s |
| after-return-chat | PASS | a chat request after the return answers |
| each-0000:01:00.0 | PASS | lend 200 {'id': '2', 'gpu': '0000:01:00.0', 'mib': 2052} (0.06 s), this GPU down 2052 MiB, others {'0000:02:00.0': 0, '0000:05:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 22374 (before 22374) |
| each-0000:02:00.0 | PASS | lend 200 {'id': '3', 'gpu': '0000:02:00.0', 'mib': 2108} (0.05 s), this GPU down 2108 MiB, others {'0000:01:00.0': 0, '0000:05:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 23504 (before 23504) |
| each-0000:05:00.0 | PASS | lend 200 {'id': '4', 'gpu': '0000:05:00.0', 'mib': 2108} (0.03 s), this GPU down 2108 MiB, others {'0000:01:00.0': 0, '0000:02:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 15232 (before 15232) |
| each-0000:09:00.0 | PASS | lend 200 {'id': '5', 'gpu': '0000:09:00.0', 'mib': 2108} (0.05 s), this GPU down 2108 MiB, others {'0000:01:00.0': 0, '0000:02:00.0': 0, '0000:05:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 15342 (before 15342) |
| each-0000:0a:00.0 | PASS | lend 200 {'id': '6', 'gpu': '0000:0a:00.0', 'mib': 2108} (0.05 s), this GPU down 2108 MiB, others {'0000:01:00.0': 0, '0000:02:00.0': 0, '0000:05:00.0': 0, '0000:09:00.0': 0}, return 200, back to 14502 (before 14502) |
| two-leases | PASS | ids 7 8; after returning the first: leases [{'id': '8', 'gpu': '0000:09:00.0', 'mib': 1054}], lent 1054 |
| busy-return | PASS | hog: dev 3 held 2369 MiB (err 0, free was 2625); return while held 409 {'error': "lease 9's memory is still in use; it stays lent - try again later", 'retry': True}, lease listed True; after free 200 {'id': '9', 'gpu': '0000:09:00.0', 'mib': 2108} |


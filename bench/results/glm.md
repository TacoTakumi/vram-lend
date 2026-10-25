# GLM-5.3-Flash: VRAM Lend live checks

Host: 2x RTX 3090 (0000:01:00.0, 0000:02:00.0), RTX 5060 Ti (0000:05:00.0), 2x RTX 4060 Ti (0000:09:00.0, 0000:0a:00.0), CUDA 13.3. Engine: the Maya fork, branch `vram-lend`, rebased on upstream v1.1.27, served through llama-swap. Memory figures are the engine process as `nvidia-smi --query-compute-apps` shows it, polled every 50 ms. Measured 2026-10-24.

Layouts: five GPUs (all of the above) and three GPUs (the two RTX 3090 and the RTX 5060 Ti). Script: `live-glm.sh` (`live.py`).

## Summary

| Check | Result | Detail |
|---|---|---|
| 8 GiB lend frees the memory | PASS | 0000:09:00.0 (RTX 4060 Ti): 8192 MiB asked, lease 8208 MiB (171 blocks of 48 MiB). Lend answer 0.36 to 0.41 s, driver shows it free 0.01 to 0.04 s after the answer |
| Return maps it back | PASS | Answer 0.05 to 0.07 s. Lendable 10320 MiB before the lend and after the return |
| Decode stall | PASS | Largest token gap 0.40 to 0.45 s across the lend, 0.16 to 0.23 s across the return (target at most 1 s) |
| Any GPU, others unchanged | PASS | 2 GiB lend and return on each of the 5 GPUs, and on each of the 3 GPUs of the three-GPU layout. Other GPUs: 0 MiB change |
| Lend while requests run | PASS | A 4096 MiB lend sent during a 12K-token prompt read was answered after 9.0 s, at the end of the prompt-path call. Return 0.69 s. Both requests finished normally |
| Full lendable keeps the prompt rate | PASS | Three-GPU layout, 0000:05:00.0 (RTX 5060 Ti) at its full lendable 7280 MiB: a 4067-token prompt read at 624 t/s against 686 t/s with nothing lent (91%). No fallback to one token at a time |
| Over lendable | PASS | 409 {"max_mib": 10320}, engine memory unchanged |
| Two leases on one GPU | PASS | Returning the first leaves the second listed, lent equals the second lease |
| Return while the memory is in use | PASS | 409 with retry while a second process held the memory, lease still listed. 200 after it freed the memory |
| Engine restart | PASS | Reload in 52 s, no leases, lent 0 on every GPU |
| Decode cost of 8 GiB lent | within noise | 6 requests before, with 8 GiB lent on 0000:09:00.0, and after: 52.50 / 50.78 / 52.21 t/s |

The engine logs each lend: 8208 MiB on 0000:09:00.0 took 332 ms, with 65 experts (511 MiB) copied to RAM inside the park budget of 512 MiB and 957 experts dropped to the file.

Two defects were found by the first runs and fixed before the rows above: an 8 GiB lend stalled decode for 3.41 s, and lendable read 2 blocks lower after a return. See the guide's lessons in [../../docs/implementing.md](../../docs/implementing.md#lessons-from-the-defects-found).

## Final run, five GPUs

| check | result | detail |
|---|---|---|
| state | PASS | /vram/state gpus ['0000:01:00.0', '0000:02:00.0', '0000:05:00.0', '0000:09:00.0', '0000:0a:00.0'], engine GPUs from nvidia-smi ['0000:01:00.0', '0000:02:00.0', '0000:05:00.0', '0000:09:00.0', '0000:0a:00.0'] |
| lend-8g-size | PASS | asked 8192, lease 8208 MiB |
| lend-8g-free | PASS | engine process on 0000:09:00.0 down 8208 MiB; driver showed >= 8192 MiB freed 0.37 s after send, 0.01 s after the answer (lend call 0.36 s) |
| lend-8g-stall | PASS | largest token gap across the lend 0.41 |
| return-8g | PASS | return 200 in 0.07 s; back to 15280 MiB (before 15280), driver showed it 0.11 s after send |
| return-8g-stall | PASS | largest token gap across the return 0.20 |
| return-8g-state | PASS | state before {'gpu': '0000:09:00.0', 'lendable': 10320, 'lent': 0} after {'gpu': '0000:09:00.0', 'lendable': 10320, 'lent': 0} |
| lend-8g-request | PASS | request finish length error None, 1496 deltas, decode 32.15 /s |
| after-return-chat | PASS | a chat request after the return answers |
| each-0000:01:00.0 | PASS | lend 200 {'id': '2', 'gpu': '0000:01:00.0', 'mib': 2058} (0.08 s), this GPU down 2058 MiB, others {'0000:02:00.0': 0, '0000:05:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 22808 (before 22808) |
| each-0000:02:00.0 | PASS | lend 200 {'id': '3', 'gpu': '0000:02:00.0', 'mib': 2048} (0.17 s), this GPU down 2048 MiB, others {'0000:01:00.0': 0, '0000:05:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 23448 (before 23448) |
| each-0000:05:00.0 | PASS | lend 200 {'id': '4', 'gpu': '0000:05:00.0', 'mib': 2064} (0.11 s), this GPU down 2064 MiB, others {'0000:01:00.0': 0, '0000:02:00.0': 0, '0000:09:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 15190 (before 15190) |
| each-0000:09:00.0 | PASS | lend 200 {'id': '5', 'gpu': '0000:09:00.0', 'mib': 2064} (0.24 s), this GPU down 2064 MiB, others {'0000:01:00.0': 0, '0000:02:00.0': 0, '0000:05:00.0': 0, '0000:0a:00.0': 0}, return 200, back to 15280 (before 15280) |
| each-0000:0a:00.0 | PASS | lend 200 {'id': '6', 'gpu': '0000:0a:00.0', 'mib': 2048} (0.22 s), this GPU down 2048 MiB, others {'0000:01:00.0': 0, '0000:02:00.0': 0, '0000:05:00.0': 0, '0000:09:00.0': 0}, return 200, back to 14472 (before 14472) |
| two-leases | PASS | ids 7 8; after returning the first: leases [{'id': '8', 'gpu': '0000:09:00.0', 'mib': 1056}], lent 1056 |
| busy-return | PASS | hog: dev 3 held 2313 MiB (err 0, free was 2569); return while held 409 {'error': "lease 9's memory is still in use; it stays lent - try again later", 'retry': True}, lease listed True; after free 200 {'id': '9', 'gpu': '0000:09:00.0', 'mib': 2064} |

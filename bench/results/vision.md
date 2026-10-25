# GLM-5.3-Flash vision: VRAM Lend smoke

Host: 2x RTX 3090 (0000:01:00.0, 0000:02:00.0), RTX 5060 Ti (0000:05:00.0), 2x RTX 4060 Ti (0000:09:00.0, 0000:0a:00.0), CUDA 13.3. Engine: the Maya fork, branch `vram-lend`, rebased on upstream v1.1.27, served through llama-swap. Memory figures are the engine process as `nvidia-smi --query-compute-apps` shows it, polled every 50 ms. Measured 2026-10-24.

The GLM vision encoder borrows its memory as a lease on the first GPU during the encode and returns it after. The test picture holds the text `CODE 7319`. Script: `vision.py`.

| Check | Result | Detail |
|---|---|---|
| Picture read | PASS | Answer `CODE 7319` in 5.2 s |
| Lease during the encode | PASS | One lease of 2772 MiB on 0000:01:00.0 (RTX 3090), seen from 0.12 s to 1.29 s after the request |
| No lease after | PASS | 21 polls after the answer, no leases |

## Final run

| check | result | detail |
|---|---|---|
| picture read | PASS | HTTP 200, answer 'CODE 7319', 5.2 s |
| lease during encode | PASS | [{'id': '10', 'gpu': '0000:01:00.0', 'mib': 2772}] seen from 0.12 s to 1.29 s after the request (12 of 44 polls at 100 ms) |
| no lease after | PASS | 21 polls after the answer, leases ['[]'] |

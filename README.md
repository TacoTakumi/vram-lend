# VRAM Lend

VRAM Lend lets an inference engine lend part of its GPU memory to another
program on the same machine while it keeps serving requests, and take the
memory back after.

A large MoE model on consumer GPUs fills every GPU, mostly with its expert
cache: copies of experts that also live in RAM or on disk. An image
generator, a speech service or a small helper model needs a few GiB on one
GPU for a short time. With VRAM Lend the engine empties that many MiB of its
expert cache on the GPU asked for and releases them to the driver at once.
The borrower allocates the memory, uses it and frees it, and a return gives
it back to the engine. The engine never stops serving.

```
POST /vram/lend   {"gpu": "0000:09:00.0", "mib": 8192}  ->  {"id": "7", "gpu": "0000:09:00.0", "mib": 8208}
POST /vram/return {"id": "7"}                           ->  {"id": "7", "gpu": "0000:09:00.0", "mib": 8208}
GET  /vram/state                                        ->  {"gpus": [...], "leases": [...]}
```

## Status

| Component | Status | Code |
|---|---|---|
| Project Maya (GLM-5.3-Flash and DeepSeek-V4.1 engines) | Done: lend and return on any GPU while requests run | Fork branch: <https://github.com/TacoTakumi/project-maya/tree/vram-lend>. Upstream PR: not opened yet |
| Strata | Planned | - |
| llama.cpp | Planned | - |
| Berth (broker side) | Planned, in design discussion | <https://github.com/T-Crypt/berth/issues/189> |

## Results

Maya fork, rebased on upstream v1.1.27, on 2x RTX 3090, RTX 5060 Ti and
2x RTX 4060 Ti. An 8 GiB lend on an RTX 4060 Ti while a request streams:

| Check | GLM-5.3-Flash | DeepSeek-V4.1 |
|---|---|---|
| Lend answer / driver shows the memory free | 0.36-0.41 s / 0.01-0.04 s after | 0.16-0.22 s / 0.01-0.05 s after |
| Largest token gap, lend / return | 0.40-0.45 s / 0.16-0.23 s | 0.23-0.28 s / 0.12-0.21 s |
| Return answer | 0.05-0.07 s | 0.08-0.13 s |
| 2 GiB on each GPU, other GPUs | all pass, 0 MiB change | all pass, 0 MiB change |
| Prompt rate at full lendable | 91% of the rate with nothing lent | 85-98% |
| Lendable before the lend = after the return | 10320 = 10320 MiB | 10478 = 10478 MiB |
| Decode with 8 GiB lent | within noise (52.5 / 50.8 / 52.2 t/s before / lent / after) | no penalty visible |

Over lendable (409), two leases on one GPU, a return while the memory is in
use (409, retry), a lend during a long prompt read, a late lease given back by
the server, and an engine restart all pass. The full tables are in
[bench/results/](bench/results/).

## Documentation

- [docs/api.md](docs/api.md): the HTTP API (`/vram/lend`, `/vram/return`,
  `/vram/state`), status codes and lease rules.
- [docs/engine-protocol.md](docs/engine-protocol.md): the control lines
  between the server and the engine (`LEND`, `RETURN`, `LENDABLE`), when the
  engine answers, answer order and late leases.
- [docs/implementing.md](docs/implementing.md): how to add VRAM Lend to an
  engine: the VMM pool, the order before an unmap, the lendable rule, and the
  lessons from the defects found.
- [docs/research/findings.md](docs/research/findings.md): prior art, where
  the memory is, what a lend costs, and why the memory must go back to the
  driver. Also [glossary.md](docs/research/glossary.md) and
  [questions.md](docs/research/questions.md).
- [bench/README.md](bench/README.md): the scripts behind the results.

## Licence

MIT, see [LICENSE](LICENSE). The Maya fork branch keeps the licence of
Project Maya.

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
| Strata | Lend and return on both GPUs of a 2x RTX 3090 layer split while requests run | Fork branch: <https://github.com/TacoTakumi/Strata/tree/vram-lend>, on [Niko1221/Strata](https://github.com/Niko1221/Strata) v0.2.6 with upstream PR [#2522](https://github.com/Niko1221/Strata/pull/2522), which now carries the four fixes from this work as its own commits. Upstream issue: [#4168](https://github.com/Niko1221/Strata/issues/4168) |
| llama.cpp | Done: lend and return of the MoE expert cache on any GPU that has one, while requests run. Served on this host through llama-swap as its own entry | Fork branch: <https://github.com/TacoTakumi/llama.cpp/tree/vram-lend>, on master 961c376a2 with the per-device `--moe-cache-mib` of [#30205](https://github.com/ggml-org/llama.cpp/pull/30205). Upstream PR: not opened yet |
| llama-swap (one address for every engine) | Done as a general change: a request matching `upstream.ignorePaths` on a loaded model is served outside the scheduler (no start, no concurrency slot, no in-flight count, no ttl reset), so `/upstream/<model>/vram/lend`, `/vram/return` and `/vram/state` with `^/vram/` in `ignorePaths` reach a serving engine and never load one. Ten lines of logic, tests, docs. See [docs/llama-swap.md](docs/llama-swap.md) | Fork branch: <https://github.com/TacoTakumi/llama-swap/tree/untracked-paths>, on [mostlygeek/llama-swap](https://github.com/mostlygeek/llama-swap) 6c855da (v267). Upstream issue: [#1135](https://github.com/mostlygeek/llama-swap/issues/1135), existing. Upstream PR: not opened yet. Replaces the earlier `vram-route` branch (a `/vram` route on llama-swap's own port, routed by card) |
| Berth (broker side) | Done on one GPU, as an experimental feature of the v0.4.0 pre-releases (rc.3 tested on this host: a 6.5 GiB voice borrower on an RTX 4060 Ti held by Maya, lend, grant and take back while Maya streams). Several GPUs under one lease are planned | [v0.4.0-rc.3](https://github.com/T-Crypt/berth/releases/tag/v0.4.0-rc.3), [contrib/LEND-PROTOCOL.md](https://github.com/T-Crypt/berth/blob/next/contrib/LEND-PROTOCOL.md). Design and tests: <https://github.com/T-Crypt/berth/issues/189> |

## Results

Maya fork, final builds before and after the rebase onto upstream v1.1.27
(the results pages give the build of each row), on 2x RTX 3090, RTX 5060 Ti
and 2x RTX 4060 Ti. An 8 GiB lend on an RTX 4060 Ti while a request streams:

| Check | GLM-5.3-Flash | DeepSeek-V4.1 |
|---|---|---|
| Lend answer / driver shows the memory free | 0.36-0.41 s / 0.01-0.04 s after | 0.16-0.22 s / 0.01-0.05 s after |
| Largest token gap, lend / return | 0.40-0.45 s / 0.16-0.23 s | 0.23-0.28 s / 0.12-0.21 s |
| Return answer | 0.05-0.07 s | 0.08-0.13 s |
| 2 GiB on each GPU, other GPUs | all pass, 0 MiB change | all pass, 0 MiB change |
| Prompt rate at full lendable | 91% of the rate with nothing lent | 85-98% |
| Lendable before the lend = after the return | 10320 = 10320 MiB | 10478 = 10478 MiB |
| Decode with 8 GiB lent | within noise (52.5 / 50.8 / 52.2 and 47.7 / 48.8 / 49.3 t/s before / lent / after) | no penalty visible |

Over lendable (409), two leases on one GPU, a return while the memory is in
use (409, retry), a lend during a long prompt read, a late lease given back by
the server, and an engine restart all pass.

Strata fork, branch `vram-lend`, Swift 1.5 IQ4_XS on a layer split over the
2x RTX 3090 (0000:01:00.0 on a 24.4 GB/s PCIe link, 0000:02:00.0 on a
6.1 GB/s link). An 8 GiB lend on each RTX 3090 while a request streams:

| Check | 0000:01:00.0 | 0000:02:00.0 |
|---|---|---|
| Lend answer / driver shows the memory free | 0.04-0.05 s / 0.01-0.04 s after | 0.04-0.05 s / 0.01-0.02 s after |
| Largest token gap, lend / return | 0.046-0.056 s / 0.029-0.039 s | 0.053-0.055 s / 0.067-0.074 s |
| Return answer | 0.01-0.02 s | 0.02-0.03 s |
| 2 GiB on each GPU, other GPUs | all pass, 0 MiB change | all pass, 0 MiB change |
| Prompt rate at full lendable | 98.5-99.7% of the rate with nothing lent | 69-73%: Failed against the 90% target, accepted as the slow link's cost |
| Lendable before the lend = after the return | 12800 = 12800 MiB | 12288 = 12288 MiB |
| Decode with 8 GiB lent | -5.6 / -6.5% (hit rate 99.6 to 98.3%) | -11.5 / -9.5% (hit rate 99.6 to 97.2%) |

A lend during a long prompt read is answered at the next prompt chunk. Over
lendable, two leases on one GPU, a return while the memory is in use, an
engine restart, 50 lend/return cycles during streaming requests and the
quality suite with 8 GiB lent on each GPU all pass. A late lend (503) was
not reachable, because the engine answers inside the server's wait; the
server's unit tests cover that path. HIP and SYCL builds were not run.

llama.cpp fork, branch `vram-lend`, the same Swift 1.5 IQ4_XS with its MTP
draft head and vision encoder on an explicit split over all five GPUs. The
experts of 14 layers stay in host RAM behind an 8192 MiB MoE expert cache on
the RTX 5060 Ti (0000:05:00.0) and on one RTX 4060 Ti (0000:09:00.0); those
two cards lend 8112 MiB each. An 8 GiB lend on 0000:05:00.0 while a request
streams:

| Check | 0000:05:00.0 |
|---|---|
| Lend answer / driver shows the memory free | 0.04-0.05 s / at or before the answer |
| Largest token gap, lend / return | 0.11 s / 0.11-0.12 s |
| Return answer | 0.03-0.10 s |
| 2 GiB on each lending GPU, other GPUs | all pass, 0 MiB change |
| Prompt rate at full lendable | 101% of the rate with nothing lent (the prompt path reads host experts directly) |
| Lendable before the lend = after the return | 8112 = 8112 MiB, 20 cycles |
| Decode with 8 GiB lent | 25.0 t/s against 58.3 t/s (the host-expert rate: a full lend leaves each expert group its floor only); back within 5% on the first 300-token run after the return |

Over lendable, two leases on one GPU, a return while the memory is in use
(409, retry), a lend during a 63k-token prompt read (answered at the next
batch, 1.2-1.4 s), a late lend (503 with a 1 ms wait), an engine restart,
and the quality suite (coding 16/16, math 12/12, needles 6/6 at 32k and 128k
with both cards fully lent) all pass. Three lend cycles under the CUDA memory
checker: 0 errors. The lend-ready layout itself costs 36% of the prompt rate
against the plain split (18 GiB of experts read from host RAM) and nothing
on decode. HIP and SYCL builds were not run. The full tables are in
[bench/results/](bench/results/).

## Documentation

- [docs/api.md](docs/api.md): the HTTP API (`/vram/lend`, `/vram/return`,
  `/vram/state`), status codes and lease rules.
- [docs/llama-swap.md](docs/llama-swap.md): the same API through
  llama-swap's `/upstream/<model>/` passthrough for every engine it runs:
  the `ignorePaths` entry, what a call does on a loaded and an unloaded
  model, the fork branch that keeps a call outside the scheduler, the Berth
  `lend_url`, status codes and common failures.
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
Project Maya, the Strata and llama.cpp fork branches the licences of those
projects.

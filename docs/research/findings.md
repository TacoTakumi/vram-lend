# Research findings

These findings led to the VRAM Lend design. They come from a prior-art scan,
from measurements on one multi-GPU host, and from a design discussion with the
author of Berth, a GPU lease broker. Terms are in [glossary.md](glossary.md).
Open points are in [questions.md](questions.md). Sources are listed at the
end, by number.

## The problem

A large MoE model served on consumer GPUs fills every GPU. Most of that memory
is the engine's expert cache: copies of experts that also live in RAM or on
disk. Other local programs need a few GiB on one GPU for a short time: an
image generator, a speech service (TTS and STT), a small helper model. Today
the only ways to get that memory are to stop the large model or to load it
smaller for all requests.

VRAM Lend lets the engine give a part of its expert cache on one GPU to such a
program while it keeps serving, and take it back after.

## Prior art

| System | What it frees | While serving | By amount, per GPU |
|---|---|---|---|
| vLLM sleep mode [1] | weights, KV cache, graphs | no | no |
| SGLang memory release [2] | weights, KV cache | no | no |
| TensorRT-LLM sleep [3] | by memory tag | no | no |
| LMDeploy weight offload [4] | weights | no | no |
| kvcached runtime limits [5] | KV cache pages | yes | by amount, per engine instance |
| llama.cpp MoE expert cache [6] | (fixed size at start, one device) | - | no |
| NVIDIA MPS [7] | (limit set at client start) | - | per device, at start only |
| MIG [8] | (not on GeForce GPUs, not resized while running) | - | - |
| HAMi, Kubernetes DRA [9] [10] | (accounting, no cooperative resize) | - | per device |
| Berth [11] | whole GPU, or an admission size | no (the holder is parked) | admission size only |

Findings from the scan:

- The engine sleep APIs release all of one kind of memory for the whole
  engine. The engine does not serve while asleep.
- kvcached is the only system found that shrinks a running engine by an amount
  while it serves. It covers the KV cache only.
- No system found gives back a chosen amount on a named GPU while the engine
  keeps serving with its weights in use, on a host with GPUs of different
  sizes.
- Berth is the closest broker: a priority lease queue, a drain at a job
  boundary, a park-to-RAM protocol, and a grant only when the driver shows the
  memory free. Its leases are whole-GPU or admission-sized, and the holder
  stops serving while parked.

## Where the memory is

The test host has five GPUs: 2x RTX 3090 (24 GiB), RTX 5060 Ti (16 GiB) and
2x RTX 4060 Ti (16 GiB). The model is GLM-5.3-Flash in the Maya engine, split
by layer ranges over two, three or five GPUs.

On every layout, each GPU holds one layer range plus an expert pool, and the
pool is most of the GPU. Five-GPU layout:

| GPU | Layers | Expert pool | Everything else |
|---|---|---|---|
| RTX 3090 (01:00.0) | 0-11 | 19.21 GB | 3.71 GB |
| RTX 3090 (02:00.0) | 12-23 | 19.63 GB | 3.30 GB |
| RTX 5060 Ti (05:00.0) | 24-31 | 12.49 GB | 2.39 GB |
| RTX 4060 Ti (09:00.0) | 32-38 | 13.01 GB | 1.95 GB |
| RTX 4060 Ti (0a:00.0) | 39-44 | 10.93 GB | 4.03 GB |

"Everything else" is dense weights, the KV cache at 262144 context,
activations and other buffers. The two- and three-GPU layouts show the same
shape (pools of 10.4 to 19.6 GB, the rest 1.95 to 5.63 GB).

Result: a lend of several GiB can come from the expert pool alone. No layer
moves, so a lend is a matter of emptying slots, not of re-planning the model.

## What a lend costs

To measure the cost before any lend code existed, a holder process took the
memory on one GPU before the engine loaded, so the engine sized its pool
smaller. Decode was measured over 31 requests.

| Layout | GPU held | Held | Decode t/s | Change |
|---|---|---|---|---|
| 5 GPUs | none | 0 | 68.05 | - |
| 5 GPUs | RTX 4060 Ti (09:00.0) | 8312 MiB | 66.39 | -2.4% |
| 5 GPUs | RTX 5060 Ti (05:00.0) | 8320 MiB | 66.07 | -2.9% |
| 3 GPUs | none | 0 | 50.20 | - |
| 3 GPUs | RTX 5060 Ti (05:00.0) | 4224 MiB | 49.99 | -0.4% |
| 3 GPUs | RTX 5060 Ti (05:00.0) | 8320 MiB | 46.15 | -8.1% |

Decode noise is about 0.5 t/s. On the five-GPU layout, answer quality on code
and math checks stayed in the spread of the baseline.

The 8 GiB case on the three-GPU layout showed a second effect. The pool
dropped to 2.31 GB, too small to lend the prompt path the 1217 MB of buffers
it borrows. Prompt reading fell back to one token at a time: a cold
1943-token prompt took 75 s, against 5.3 s with nothing held.

Result: lending costs little until a GPU's pool nears its floor, about
0.2 t/s per GiB on the five-GPU layout up to 8 GiB. The floor is not only the
minimum slots a layer keeps. The pool must also keep what the prompt path
borrows. This gives the lendable rule:

```
lendable = pool - prompt-path borrow - minimum slots, rounded down to whole blocks
```

## The memory must go back to the driver

A broker such as Berth grants memory to a borrower only after a driver read
(NVML) shows it free [11] [12]. An engine that frees slots into its own
allocator pool would never pass that check. So a lend must release the memory
to the driver, and a return must allocate it again.

Plain `cudaFree` and `cudaMalloc` would change slot addresses. The CUDA
virtual memory management API keeps a reserved address range and maps and
unmaps physical blocks inside it [13], so addresses stay fixed. VRAM Lend
uses it (see [../implementing.md](../implementing.md)). `cuMemUnmap` does not
wait for kernels in flight, so the engine must synchronize first. A race of
this kind is reported in [14].

## An earlier in-process lend

Maya v1.1.25 already lent part of its expert pool to its own vision encoder
on the first GPU [15]. It freed a pool tail with `cudaFree` and allocated it
again after the encode. Timed against `nvidia-smi` on an RTX 3090 over six
lends with the engine idle:

- 2758 MiB were free 0.10 to 0.19 s after the picture request (the poll was
  about 140 ms, and the figure includes HTTP and server time).
- The engine was back at full size 0.93 to 1.22 s after the request.

That lend had three limits: a size fixed at load, the first GPU only, and on
a multi-GPU engine only while idle. VRAM Lend removes all three. The final
VRAM Lend measurements are in [../../bench/results/](../../bench/results/).

## Broker integration

The broker side is discussed in Berth issue #189 [12]. Points agreed there:

- A GPU is named by its PCI address.
- A lend over what the engine can give answers 409 with the amount it can
  give (`max_mib`).
- The state call reports lendable per GPU beside what is lent.
- One lease covers several GPUs with one share per GPU, because one engine
  process can hold all the GPUs of the host. Berth plans this before the lend
  calls.
- A lend or grant never takes a GPU below a per-GPU reserve, for example on
  the GPU that drives the display.

The engine side of VRAM Lend ([../api.md](../api.md)) uses its own route
names and lease ids. A broker adapter maps its calls to them.

## Sources

1. vLLM sleep mode: <https://docs.vllm.ai/en/latest/features/sleep_mode>
2. SGLang for RL (memory release and resume): <https://sgl-project.github.io/advanced_features/sglang_for_rl.html>
3. TensorRT-LLM memory: <https://nvidia.github.io/TensorRT-LLM/reference/memory.html>
4. LMDeploy update weights: <https://lmdeploy.readthedocs.io/en/latest/advance/update_weights.html>
5. kvcached memory control: <https://github.com/ovg-project/kvcached/tree/main/examples/02_memory_control>
6. llama.cpp MoE expert cache, PR 29887: <https://github.com/ggml-org/llama.cpp/pull/29887>
7. NVIDIA MPS overview: <https://docs.nvidia.com/deploy/pdf/CUDA_Multi_Process_Service_Overview.pdf>
8. MIG supported GPUs: <https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-gpus.html>
9. HAMi: <https://github.com/project-hami/hami>
10. Kubernetes DRA partitionable devices: <https://github.com/kubernetes/enhancements/blob/master/keps/sig-scheduling/4815-dra-partitionable-devices/README.md>
11. Berth: <https://github.com/T-Crypt/berth>
12. Berth issue 189, lending part of a GPU from a running engine: <https://github.com/T-Crypt/berth/issues/189>
13. CUDA virtual memory management: <https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/virtual-memory-management.html>
14. PyTorch issue 196258, unmap race: <https://github.com/pytorch/pytorch/issues/196258>
15. Project Maya: <https://github.com/mw00/project-maya>

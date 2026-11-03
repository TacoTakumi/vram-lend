# VRAM Lend HTTP API

This page specifies the HTTP API of VRAM Lend. A program on the same machine
(the borrower) asks the inference server for part of the GPU memory that the
engine uses as its expert cache, uses that memory, and gives it back. The
engine keeps serving requests while memory is lent.

The server turns each call into a control line to the engine and the answer
into JSON. The control lines are specified in
[engine-protocol.md](engine-protocol.md). The same API through llama-swap's
`/upstream/<model>/` passthrough, one address for every engine it runs, is
in [llama-swap.md](llama-swap.md).

## Terms

- **GPU**: one device, named by its PCI address.
- **Lease**: memory that one lend took from one GPU. A lease has an id, a GPU
  and a size in MiB. The server does not record who holds a lease.
- **Block**: the unit of a lend on a GPU. A lend is rounded up to whole blocks.
  The block size depends on the engine and the GPU (16 to 64 MiB in the Maya
  implementation).
- **Lendable**: how many MiB a GPU can lend now, a multiple of its block size.
- **Lent**: how many MiB of a GPU the open leases hold.
- **Whole-card lend**: a lend that takes the whole GPU. The engine destroys
  its part on that GPU, so the GPU is free for another process. The size is
  the GPU's `whole_mib`, its footprint.

## GPU names

The `gpu` field is the GPU's PCI address. The server accepts CUDA's form
(`0000:0A:00.0`), NVML's 8-digit domain (`00000000:0A:00.0`) and lspci's form
without a domain (`0a:00.0`), in any letter case. It answers with one form:
lower case, a 4-digit domain and the function, as lspci and sysfs show it
(`0000:0a:00.0`).

The server maps the address to the engine's own device number. A borrower
never sees device numbers.

## POST /vram/lend

Request:

```json
{"gpu": "0000:0a:00.0", "mib": 8192}
```

- `gpu`: the PCI address of a GPU the engine uses.
- `mib`: a whole number of MiB above 0, or the string `"all"` for the whole
  GPU. A boolean is not a number here. `"all"` is the one string accepted;
  `"ALL "` or any other string is invalid.

Answers:

| Status | Body | When |
|---|---|---|
| 200 | `{"id": "7", "gpu": "0000:0a:00.0", "mib": 8192}` | The memory is free for the borrower. `mib` is the size lent, rounded up to whole blocks, so it can be larger than the request. For a whole-card lend, `mib` is the footprint released |
| 400 | `{"error": "..."}` | The body is not a JSON object; `mib` is missing, not a whole number, 0 or negative (`true`, `"ALL "`, `0`, `-1`); `gpu` is not a PCI address or not a GPU the engine uses |
| 409 | `{"max_mib": N}` | The request is more than the GPU can lend now. `N` is its lendable. A GPU of the engine that does not lend answers `N` = 0. See [Whole-card lend](#whole-card-lend) for its refusals |
| 501 | `{"error": "..."}` | The engine cannot lend |
| 502 | `{"error": "..."}` | The engine gave an answer the server does not know |
| 503 | `{"error": "..."}` | The engine is not running, did not answer in time, or restarted during the call |

When the 200 answer comes, the memory is already released to the driver. In
the Maya implementation the driver shows it free within 0.05 s of the answer;
in the llama.cpp implementation at or before the answer.

A lend that the engine does not answer within the server's wait (30 s in the
Maya and llama.cpp implementations; `--vram-lend-wait-ms` in llama.cpp)
answers 503. The engine can still carry out that lend
later. When it does, the server gives the lease back by itself, and
`/vram/state` lists the lease until the engine has it back. A borrower that
gets a 503 must not use the memory.

### Whole-card lend

A GPU with a `whole_mib` in `/vram/state` can be lent whole. Two requests do
it:

- `"mib": "all"`.
- A `mib` above the GPU's lendable and at most its `whole_mib`.

Both answer 200 with `id`, `gpu` and `mib` = the footprint released, the
GPU's `whole_mib`. The lease is returned with the normal `/vram/return`.

Refusals, all with nothing changed:

| Status | Body | When |
|---|---|---|
| 409 | `{"max_mib": N}` | `"all"` on a GPU without `whole_mib`. `N` is its lendable |
| 409 | `{"max_mib": N}` | A number above `whole_mib`. `N` is `whole_mib` |
| 409 | `{"max_mib": N}` | A whole-card lend while any lease is open on that GPU. `N` is its lendable |
| 409 | `{"max_mib": 0}` | A lend of a part while a whole-card lease is open on that GPU |

The two kinds of lease never share a GPU. While a whole-card lease is open on
a GPU, its lendable is 0 and its lent equals the lease.

A whole-card lend takes longer than a pool lend: the engine destroys its part
on the GPU. The route waits up to 180 s for the engine's answer (30 s for a
pool lend, unchanged). A late answer after the route gave up is handled as
for a pool lend: the server gives the lease back by itself. A borrower should
use a lend and return timeout of at least 200 s.

## POST /vram/return

Request:

```json
{"id": "7"}
```

- `id`: a lease id as `/vram/lend` answered it. A JSON number is also
  accepted.

Answers:

| Status | Body | When |
|---|---|---|
| 200 | `{"id": "7", "gpu": "0000:0a:00.0", "mib": 8192}` | The engine has the memory back |
| 400 | `{"error": "..."}` | The body is not a JSON object, or `id` is missing or is not one word |
| 404 | `{"error": "..."}` | No open lease has this id: it was never lent, it was returned already, or the engine process that lent it has ended. The server sends nothing to the engine |
| 409 | `{"error": "...", "retry": true}` | The memory is still in use by another process. The lease stays open. Free the memory and return again |
| 501 | `{"error": "..."}` | The engine cannot lend |
| 502 | `{"error": "..."}` | The engine gave an answer the server does not know |
| 503 | `{"error": "..."}` | The engine did not answer |

A return gives back the whole lease. There is no partial return.

## GET /vram/state

Answer, always 200:

```json
{
  "gpus": [
    {"gpu": "0000:01:00.0", "lendable": 0, "lent": 0},
    {"gpu": "0000:0a:00.0", "lendable": 2128, "lent": 8192, "whole_mib": 15360}
  ],
  "leases": [
    {"id": "7", "gpu": "0000:0a:00.0", "mib": 8192}
  ]
}
```

- `gpus`: every GPU the engine uses, with its lendable and lent in MiB. A GPU
  that does not lend shows 0 and 0.
- `whole_mib`: the footprint a whole-card lend of that GPU releases, in MiB.
  Present for a GPU the engine can lend whole and equal to the engine's
  figure; absent for every other GPU. While a whole-card lease is open on a
  GPU, its lendable is 0 and its lent equals the lease.
- `leases`: the open leases, in the order they were lent. A lease that the
  server is giving back by itself is still listed until the engine has it back.

When the engine is not running or cannot lend, every GPU shows 0 and 0 and
`leases` is empty.

The `leases` list always agrees with the `lent` figures: the server updates its
record in the order the engine answered.

## Lease ids and lifetime

- A lease id is an opaque string. Several leases can be open on one GPU, and
  returning one leaves the others lent.
- An id names one lease for the life of the server. After an engine restart,
  an id from before the restart answers 404. It never names a lease of the new
  engine process.
- Leases end with the engine process. After a restart, an idle unload or a
  crash, `/vram/state` shows no leases and nothing lent. The memory went back
  to the driver with the process.

## What a borrower can rely on

- After a 200 lend, the borrower can allocate up to `mib` MiB on that GPU,
  less what other processes take first.
- The engine keeps serving during the lend and while the lease is open. Its
  prompt reading is not pushed back to one token at a time, because lendable
  keeps the memory the prompt path needs.
- The borrower frees its allocation before it returns the lease. A return
  while the memory is still allocated answers 409 and changes nothing.

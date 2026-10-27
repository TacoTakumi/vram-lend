# Implementing VRAM Lend in an engine

This guide is for the developer who adds VRAM Lend to an engine that does not
have it. It describes what the engine must do so that a lend frees GPU memory
at once, while requests run, and so that a return gives it back without
breaking the engine. The wire formats are in [api.md](api.md) and
[engine-protocol.md](engine-protocol.md).

The reference implementation is the Maya fork, branch `vram-lend`
(<https://github.com/TacoTakumi/project-maya/tree/vram-lend>). Its GLM-5.3-Flash
and DeepSeek-V4.1 engines share one lend layer. The shared parts are in
`include/strata/core/vram_pool.hpp` and `include/strata/core/vram_lend.hpp`.
Each engine implements one small interface over its own expert cache.

A second implementation is the llama.cpp fork, branch `vram-lend`
(<https://github.com/TacoTakumi/llama.cpp/tree/vram-lend>). It differs from
the guide below in two ways. There is no separate engine process: llama-server
calls three library functions (lendable, lend, return) and runs each lend or
return as a server task between two batches, so the engine protocol does not
apply. And there is no VMM pool: a lend frees the device's expert cache buffer
and allocates it again with fewer slots, rebuilding that device's cache lists
and slot maps, and a return allocates the old slot count again. This is
simpler and it measured as fast as the pool (a lend of 8 GiB in 0.04 s, a
token gap of 0.11 s), because the cache holds no pointers that outlive a
rebuild (its CUDA graphs are captured again after one). It only works for a
cache that can be rebuilt between two batches; an engine whose kernels or
graphs hold slot addresses across steps needs the pool.

## What can be lent

An MoE engine that offloads experts keeps a cache of expert slots in VRAM and
the full set in RAM or on disk. A slot in the cache can be emptied at a cost:
the next route that wants its expert reads it from RAM or disk. That cache is
the memory to lend. Weights that have no copy outside VRAM (dense layers,
attention, layers that keep every expert resident) cannot be lent, because
nothing can serve them while they are gone.

## The VMM pool

`cudaFree` of part of a cache and a later `cudaMalloc` do not give back the
same addresses. Every pointer to a slot, every slot table on the device and
every captured CUDA graph would need to change. The CUDA virtual memory
management (VMM) API avoids this.

1. **Reserved range.** At load, the engine reserves one virtual address range
   per GPU for all its cached expert slots (`cuMemAddressReserve`).
2. **Whole-slot blocks.** The engine backs the range with physical blocks of
   one size (`cuMemCreate`, `cuMemMap`, `cuMemSetAccess` for the owning GPU
   only). A block size is a multiple of the GPU's allocation granularity
   (2 MiB on RTX 3090, RTX 4060 Ti and RTX 5060 Ti) and holds whole slots: no
   slot crosses a block edge, so an unmapped block takes out whole slots and
   nothing else. The Maya pool chooses a size between 16 and 64 MiB that wastes
   the least.
3. **Fixed addresses.** A lend unmaps whole blocks and releases them
   (`cuMemUnmap`, `cuMemRelease`). The driver shows the memory free at once. A
   return creates new physical blocks and maps them at the same addresses.
   Slot pointers, device tables and CUDA graphs stay valid.

A return must be all or nothing. When the driver cannot back one block,
because another process still holds the memory, the engine releases the blocks
that this return already mapped and answers busy. The lease stays open.

Where VMM is not available (a HIP build, a GPU without VMM support), the same
layout can live in one plain allocation that never lends. The engine then
reports nothing lendable.

## The order before an unmap

`cuMemUnmap` does not wait for kernels in flight. A kernel that reads an
unmapped slot faults or reads garbage. The engine must keep this order for
each lend:

1. **Quiet the cache.** Stop new background moves (promotions, prefetches) and
   let the moves in flight land.
2. **Slots out of the tables.** Choose the blocks. Take their slots out of the
   host and device slot tables, so no picker can hand them out, and move their
   experts to other slots, to RAM or to disk.
3. **Device sync.** Synchronize every stream that can touch the pool on that
   GPU: compute, copy, prefetch, the prompt path, draft and MTP streams. A full
   `cudaDeviceSynchronize` on the GPU is the simple and safe choice.
4. **Unmap.** Unmap and release the blocks.
5. **Resume.** Allow background moves again.

A return is the reverse: map the blocks, then mark their slots free in the
tables. The cache fills them again as routes ask.

## The lendable rule and its floor

A lend must never push the engine into a slower mode. The lendable figure of a
GPU is:

```
lendable = pool - prompt_borrow - minimum, clamped at 0, rounded down to whole blocks
```

- `pool`: the cached pool as it is now, after the blocks already lent have
  left it. Each open lease shrinks lendable by its own size, and a return
  grows it again.
- `prompt_borrow`: what the prompt path borrows from the pool for its buffers.
  Without it, prompt reading falls back to one token at a time.
- `minimum`: the floor of slots each layer keeps: at least one route's
  experts, the spares the cache needs, and any slot with a fixed role.

The engine checks a request against lendable before it rounds the request up
to whole blocks, so a lend never takes more than lendable.

Lendable must follow the layout only: which blocks are mapped and what the
floor is. It must not follow which blocks the engine would choose by heat.
See [Lendable drift](#lendable-drift-from-heat-and-from-a-moving-spare).

## Answering lend lines between steps

The engine's input reader puts each lend command in one queue. The engine's
main thread answers the queue:

- `LENDABLE` at any point, also between two tokens.
- `LEND` and `RETURN` only where nothing is in flight on any GPU: at once when
  idle, between two scheduler steps after every issued token has completed,
  and between two prompt-path calls while a prompt is read.

The decode loop checks the queue once per token (a lock-free counter is
enough). When a `LEND` or `RETURN` waits, the decode stops at the next token
boundary, the main thread answers, and the decode goes on. A lend must be
short: an 8 GiB lend in the Maya engines stops decode for 0.23 to 0.45 s.

## In-order answer matching

The engine answers lend commands in the order they came, from one thread, and
the answers carry no tag. The server matches each answer to the oldest
command without an answer. The server must keep this rule even for a command
whose HTTP caller has stopped waiting. Otherwise a late answer is taken by the
next command, and both answers go to the wrong caller. See
[Orphaned late leases](#orphaned-late-leases).

## Lessons from the defects found

The live checks and the code reviews of the Maya implementation found these
defects. Each one is a trap for a new implementation.

### Decode stall from parking with decode stopped

The first GLM 8 GiB lend stalled decode for 3.41 s. The lend copied every
expert of the lent slots to RAM (669 experts) while decode was stopped. A
device-to-host copy of 8 GiB over a PCIe 4.0 x4 link took 1.9 s of that.

Fix: a park budget. The lend copies to RAM only up to a budget (512 MiB by
default in the Maya GLM engine), hottest experts first. The rest are dropped
and read from disk when a route wants them. A dropped expert costs nothing
during the lend.

### Re-read after a draft-model stop

The rest of that stall was a 110-token re-read after the lend. The decode had
stopped inside a DFlash speculative step, and the engine read the accepted
tokens again after the lend.

Fix: a decode that stops for a lend leaves the state at every written token
except the last, as after a prompt's last token. After the lend the engine
reads only that one token. With both fixes the 8 GiB lend stall dropped to
0.40 s.

### Lendable drift from heat and from a moving spare

After a lend and its return, the reported lendable was different from before
the lend in both engines. Lendable was computed from the blocks the engine
would choose, and that choice follows expert heat, which changes all the time.
In the DeepSeek engine a swap spare slot also moved to a new block with every
swap, and lendable moved with it.

Fix: lendable follows the layout only. The spare is a role that the floor
keeps a slot for, wherever the spare is. After the fix, lendable before a lend
and after its return were equal in every check.

### Orphaned late leases

A lend that the server stopped waiting for (a long prompt-path call) still ran
in the engine. Its lease had no owner, and its late answer was taken as the
answer of the next command.

Fix: in-order answer matching on the server, and an automatic give-back of a
lease whose caller stopped waiting (see
[engine-protocol.md](engine-protocol.md#late-leases)). Verified with a lend
during an 80K-token prompt.

### Stale ids after a restart

The engine numbers leases from 1 in each process. After an engine restart, an
old lease id could name a new lease, so a return of the old id could give
back another borrower's memory.

Fix: the server adds the highest id of earlier engine processes to each new
id, so an id names one lease for the server's life. An old id answers 404.

### Copy-slot leak after a return

In the DeepSeek engine's RAM copy mode, each fill of a slot after a return
leaked one RAM copy slot. Over many lends the RAM tier shrank.

Fix: RAM copy slots on a per-layer free list, so a fill after a return, and a
failed copy, give their copy slot back. Checked live with copy mode on and the
swap checks on.

## Checks for a new implementation

These checks found the defects above. A new implementation should pass all of
them on real models:

1. An 8 GiB lend and its return on one GPU while a request streams: the
   answer time, the time until the driver shows the memory free, and the
   largest gap between streamed tokens (target: at most 1 s).
2. A 2 GiB lend and return on each GPU in turn: the other GPUs change by less
   than one block.
3. A lend of the full lendable, then a long prompt: prompt reading stays near
   its rate with nothing lent (91% and 85 to 98% in the Maya engines).
4. Lendable before a lend equals lendable after its return.
5. A lend while a long prompt is read: answered at the end of the prompt-path
   call; past the server's wait, 503 and the lease given back by the server.
6. A lend over lendable (409), two leases on one GPU, a return while another
   process holds the memory (409, retry), and an engine restart (no leases, old
   ids 404).
7. Decode rate before, during and after an 8 GiB lend.

The scripts for these checks are in [../bench/](../bench/).

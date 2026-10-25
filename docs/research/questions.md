# Research questions

The questions of the research, answered and still open. Findings and sources
are in [findings.md](findings.md).

## Answered

### Does a lend ask for an amount or a percent?

An amount in MiB on a named GPU. The engine does not need to know what the
memory is for. GPUs differ in size, and each borrower needs a fixed amount.

### Where does lent memory come from?

From the expert pool of the GPU. On every layout measured, each GPU holds one
layer range plus an expert pool that is most of the GPU, so a lend moves no
layers.

### What does a lend cost the engine?

Little until the pool nears its floor: about 0.2 t/s of decode per GiB on the
five-GPU layout up to 8 GiB. Past the floor the prompt path cannot borrow its
buffers and prompt reading falls back to one token at a time. The lendable
rule keeps every lend above the floor.

### Must a lend release the memory to the driver?

Yes. A broker grants only after the driver shows the memory free. An engine
that keeps freed slots in its own allocator never passes that check. VRAM Lend
unmaps the blocks, so the driver shows them free at once (0.01 to 0.05 s after
the answer in the measurements).

### Who keeps the record of who holds what?

A broker on the host, not the engines and not a model switcher. The engine
only lends and returns by lease id and does not know who borrows. Berth is the
broker that the design builds on.

### Who asks for a borrower's memory?

By default, the program that starts the borrower asks before it starts it and
returns the lease when the borrower ends. A borrower that knows the broker can
also ask for itself. The memory must be free before the borrower allocates.

## Open

### Can one spec cover every engine memory layout?

The spec covers engines with an expert cache that has a copy in RAM or on
disk. Engines that split by layers without such a cache (for example
llama.cpp with layer offload), or that hold experts only in VRAM, cannot free
memory without moving layers or dropping weights. Whether that becomes an
optional slower kind of lend, and what it costs, is open.

### How much memory does each kind of borrower need?

Measured so far: a speech service with one TTS and one STT engine needs about
6.5 GiB per session. The sizes of image generation workflows and of small
helper models are not measured yet.

### Partial return

A return gives back the whole lease. A return of part of a lease, by id and
size, is possible later.

### Broker side

The lend and return calls on the broker, and one lease over several GPUs with
a share per GPU, are planned in Berth. Their final form is open.

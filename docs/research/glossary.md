# Glossary

The terms of VRAM Lend, as the spec, the guide and the research use them.

| Term | Meaning |
|---|---|
| Block | The unit of a lend on a GPU: a fixed-size piece of the expert pool that holds whole slots. A lend is rounded up to whole blocks |
| Borrower | A program that asks for GPU memory through a lend and uses it, for example an image generator or a speech service |
| Broker | A program on the host that keeps the lease ledger for every GPU and decides who gets memory, for example Berth |
| Driver check | A read of the GPU driver's own figures (NVML, `nvidia-smi`) that shows the memory free. A broker grants only after it |
| Expert cache, expert pool | The VRAM an MoE engine fills with copies of experts that also live in RAM or on disk. The memory that VRAM Lend lends |
| Floor | What the expert pool of a GPU must keep: the minimum slots per layer plus the prompt-path borrow |
| Lease | The memory that one lend took from one GPU: an id, a GPU and a size. It is open until its return |
| Lend | Taking memory out of the expert pool and releasing it to the driver, so a borrower can allocate it |
| Lendable | How many MiB a GPU can lend now: the pool minus the floor, rounded down to whole blocks |
| Lent | How many MiB of a GPU the open leases hold |
| Minimum slots | The slots each layer keeps whatever is lent: one route's experts, the spares and any slot with a fixed role |
| Park | A broker's call that makes an engine release all its memory and stop serving until resumed. A lend is a partial alternative that keeps the engine serving |
| Prompt-path borrow | The buffers the prompt path takes from the expert pool while it reads a prompt. Without them prompt reading falls back to one token at a time |
| Return | Giving a lease's memory back: the engine maps the blocks again and fills the slots as routes ask |
| Slot | The VRAM space for one expert of one layer in the expert pool |
| VMM | The CUDA virtual memory management API: a reserved address range with physical blocks mapped and unmapped inside it, so addresses stay fixed |

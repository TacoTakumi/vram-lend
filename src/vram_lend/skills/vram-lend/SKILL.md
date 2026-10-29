---
name: vram-lend
description: Lend yourself GPU memory from the inference engine that is serving, run a short job, and give the memory back. Use when a job on this machine needs VRAM while a model is served - CUDA out of memory, a GPU with too little free memory, an Android emulator with -gpu host that fails to start, a voice or image borrower, a small test model, a bench. Do NOT use to free a GPU for ComfyUI under llama-swap (llama-swap decides what runs beside it), and never to stop, swap or restart the model.
---

# vram-lend

The engine that serves the model holds GPU memory as its expert cache. Through
VRAM Lend it gives part of that memory to another process on request and keeps
serving, a little slower, until the process gives it back. The `vram-lend`
command does the asking: it finds the engine, directly or behind llama-swap,
lends on the card you name, runs your job and returns the lease when the job
exits.

Install once: `pipx install vram-lend` (or `pip install vram-lend`). Check:
`vram-lend --help`.

## Steps

1. Find the card the job will use and its PCI address:

   ```
   nvidia-smi --query-gpu=index,pci.bus_id,name,memory.free --format=csv
   ```

   A CUDA job uses the device it is pinned to (`CUDA_VISIBLE_DEVICES`). A job
   that draws through the display, such as the Android emulator with
   `-gpu host`, uses the GPU that drives the display. The address goes in as
   `0000:0a:00.0`, `0A:00.0` or the NVML form; the answer is always
   `0000:0a:00.0`.

2. See what can be lent:

   ```
   vram-lend state
   ```

   It lists every lend endpoint it found with each card's `lendable` and
   `lent` MiB and the open leases, and a note for every server that resolved
   to nothing. The default server is `http://127.0.0.1:8080`. When the engine
   or llama-swap is elsewhere, pass `--server URL` (repeatable) or set
   `VRAM_LEND_SERVER`. When llama-swap has an API key, set
   `VRAM_LEND_API_KEY`.

3. Run the job under a lease. Ask for what the job needs plus a margin, not
   the whole lendable:

   ```
   vram-lend run --gpu 0000:0a:00.0 --mib 2048 <command and arguments>
   ```

   The command and its arguments follow the options. When the command itself
   starts with a dash, put `--` before it. `run` lends, starts the command with `VRAM_LEND_LEASE`, `VRAM_LEND_MIB`,
   `VRAM_LEND_GPU` and `VRAM_LEND_ENDPOINT` in its environment, waits, and
   returns the lease when the command exits, also on a signal or a failure.
   It exits with the command's exit code. A Ctrl-C or SIGTERM to `vram-lend`
   goes to the command; a second Ctrl-C or SIGTERM kills the command with
   SIGKILL, so a job that ignores the first cannot hold the lease. Prefer
   `run`: the return is then not your job.

4. For a job that runs in the background and is stopped later (an emulator,
   a server), lend and return by hand:

   ```
   vram-lend lend --gpu 0000:0a:00.0 --mib 2048
   ```

   prints the lease as JSON: `{"id": "7", "gpu": "0000:0a:00.0", "mib":
   2048, "endpoint": "..."}`. Record the id and the endpoint: ids are per
   engine, and behind llama-swap two engines can both have a lease 7. Start
   the job only after this succeeded. When the job has exited and its memory
   is free:

   ```
   vram-lend return --id 7 --endpoint <the endpoint from the lend>
   ```

   A lease ends with the engine process, so a return after the model was
   unloaded, swapped or restarted reports the lease as already gone and exits
   0. That is not an error.

## What a refusal means

Every failure is one line on stderr. Exit 1: nothing was lent, the job must
not start. Exit 2: wrong arguments. Exit 3: a lease may still be open, return
it again.

| Message | Meaning | Next step |
|---|---|---|
| `no endpoint lists GPU ...` | No serving engine lends on that card | Run `vram-lend state`. Use another card, or tell the user; do not swap the model |
| `can lend at most N MiB` (409) | The card has less lendable than you asked | Ask for at most N, or use another card |
| `the model is not loaded` (409) | llama-swap has no loaded model there and started nothing | Tell the user. Never load a model to lend from it |
| `too many requests` (429) | Released llama-swap with a low concurrency limit refused the call while a request streams | `vram-lend` retried already; wait for the request to end and run again |
| `did not answer in time` (503) | The engine did not answer the lend | Do not use the memory. Try again later |
| `cannot lend` (501), `no /vram routes` (404) | The engine runs without VRAM Lend | Stop and tell the user which engine is serving |
| `set VRAM_LEND_API_KEY` (401) | llama-swap wants its API key | Ask the user for the key |
| `no VRAM Lend API (... on /vram/state, ... on /running)` | The server is neither an engine with VRAM Lend nor llama-swap | Ask the user for the engine or llama-swap port; pass `--server` |
| `still in use` (409 retry on a return) | The borrower still holds memory on the card | Stop the job, wait for its memory to free, return again |

## Rules

- Never start, load, swap, stop or restart a model to get memory. A lend is
  for an engine that is serving. llama-swap starts nothing for a lend call,
  and neither do you.
- Never search the process list or guess ports. When the default server
  answers nothing, ask the user for the engine or llama-swap address.
- Never use memory after a 503 or any non-zero exit of `lend` or `run`.
- A `lend` without a `return` leaves the engine slower until it restarts.
  Return every lease you took. `run` does it for you.
- When nothing lends, stop and report. Do not work around it.
- ComfyUI under llama-swap is not a case for this skill: llama-swap decides
  which models run beside ComfyUI, and a lend does not change that. Use the
  llama-swap entry made for ComfyUI instead.
- Decode slows while memory is lent, more the more you take. Ask for what
  the job needs, and return early.

## Without the command

The command is one Python file with no dependencies: copy it onto PATH as
`vram-lend` when pip is not an option. The three HTTP calls it makes are
specified in the VRAM Lend docs (`docs/api.md` and `docs/llama-swap.md` of
the vram-lend repository). By hand:

Engine on its own port (PORT is the engine's):

```
curl -s http://127.0.0.1:PORT/vram/state
curl -s -X POST http://127.0.0.1:PORT/vram/lend -d '{"gpu":"0000:0a:00.0","mib":2048}'
curl -s -X POST http://127.0.0.1:PORT/vram/return -d '{"id":"7"}'
```

Through llama-swap (MODEL is a model `GET /running` lists as `ready`; never
another):

```
curl -s http://127.0.0.1:8080/upstream/MODEL/vram/state
curl -s -X POST http://127.0.0.1:8080/upstream/MODEL/vram/lend -d '{"gpu":"0000:0a:00.0","mib":2048}'
curl -s -X POST http://127.0.0.1:8080/upstream/MODEL/vram/return -d '{"id":"7"}'
```

Start the job only on a 200 from the lend. A 409 with `max_mib` is the most
the card can lend. A 409 without it means the model is not loaded. A 503 means
do not use the memory.

# VRAM Lend through llama-swap

[llama-swap](https://github.com/mostlygeek/llama-swap) runs several inference
engines behind one port and starts and stops them on demand. A llama-swap
fork serves the VRAM Lend routes on that port: a borrower sends
`/vram/lend`, `/vram/return` and `/vram/state` to llama-swap, and llama-swap
finds the engine that holds the card. The borrower does not need to know
which model runs on which GPU, or the engine's own port.

The bodies are those of [api.md](api.md). This page gives what llama-swap
adds: which models it asks, the lease id prefix and its own status codes.

## Config

A model takes part only with `vramLend: true` (default `false`). Set it on
each model whose engine serves the VRAM Lend routes:

```yaml
models:
  big-moe:
    cmd: >
      /home/you/llama.cpp/build/bin/llama-server --port ${PORT}
      -m /home/you/models/big-moe.gguf
      --moe-cache-mib 8192 --vram-lend-max-mib 8192
    vramLend: true
```

llama-swap sends no `/vram` request of any kind to a model without the
option. The engine flags above are those of the llama.cpp fork branch; see
[bench/results/llamacpp.md](../bench/results/llamacpp.md) for a full
five-GPU layout.

## What llama-swap never does

- No `/vram` call loads, starts or swaps a model. A call reaches an engine
  only while its model is ready. A model that is stopped, loading or
  stopping is treated as if it held no card.
- No `/vram` call waits for the model's concurrency limit. A lend reaches the
  engine while it serves requests, as it does on the engine's own port.
- llama-swap does not record leases. The engine owns them, as on its own
  port.

The routes sit behind the same API-key check as `/running` and `/unload`.
When `apiKeys` is set, a call without a key answers 401. The key goes in
`Authorization: Bearer <key>`, as the password of `Authorization: Basic`, or
in `x-api-key`.

## Routing by card

When a lend model becomes ready, llama-swap reads its `/vram/state` and keeps
the model's card list for that process run. A model that restarts on other
cards is routed by its new list. A list that could not be read is read again
at the next lend. A lend waits for a list that is still being read only when
no model with a known list holds the card.

A lend goes to the ready lend models whose list holds the card, asked one at
a time in model-id order:

- A 200 ends the search. The borrower gets that answer.
- A 409 moves on to the next model.
- A model that stops during the lend is skipped the same way.
- Any other engine answer ends the search and is passed through unchanged.

Two models on one card is the normal case when a large model and a helper
model share a GPU. Each answer of `/vram/state` carries a `model` field, so a
borrower can see which model can lend how much.

## Lease ids

llama-swap rewrites the id of a 200 lend answer to
`<model id>:<run>:<engine id>`. An engine lease `7` of the model `big-moe`
becomes `big-moe:3fa94c1e:7`. A return sends that id back to llama-swap,
which strips the prefix, sends the engine id alone to that model, and
prefixes the id of a 200 answer again.

The run is a random token of 8 hex digits. llama-swap picks a new one each
time the model becomes ready and keeps it until the model leaves the ready
state. Engines start their lease ids again in each run, so the token keeps
an id from an earlier run from naming a lease of the current one: a return
whose run is not the model's current run answers 404 and reaches no engine.

The prefixed id is always a JSON string. Model ids and engine ids may both
contain colons: the model is the longest lend model id that, followed by
`:`, starts the lease id. The run follows it up to the next `:`, and the
rest is the engine id.

## POST /vram/lend

```sh
curl -s -X POST localhost:8080/vram/lend \
  -H 'Content-Type: application/json' \
  -d '{"gpu": "0000:09:00.0", "mib": 8192}'
```

```json
{"id": "big-moe:3fa94c1e:7", "gpu": "0000:09:00.0", "mib": 8192}
```

`gpu` is accepted as `0000:0a:00.0`, nvidia-smi's `00000000:0A:00.0` or
lspci's `0a:00.0`, in any letter case. llama-swap uses the address only to
find the models. The body reaches the engine unchanged.

| Status | Body | When |
|---|---|---|
| 200 | the engine's lease, id prefixed | A model lent the memory |
| 400 | `{"error": "..."}` | The body is not a JSON object, `gpu` is missing, not a string or not a PCI address, or `mib` is not a whole number above 0. No engine is asked |
| 401 | | API keys are set and the call has none |
| 409 | `{"max_mib": N}` | Every model asked refused. `N` is the largest `max_mib` among the refusals |
| 409 | `{"max_mib": 0}` | No model can serve the lend: no lend model is ready, none lists the card, or the model stopped during the lend |
| 502 | `{"error": "..."}` | The engine answered 200 with no usable id. The engine may hold a lease that the borrower cannot return |
| other | the engine's answer | The engine answered something other than 200 or 409, for example 501 or 503. A 502 or 503 from a model that stopped during the lend is not passed through |

## POST /vram/return

```sh
curl -s -X POST localhost:8080/vram/return \
  -H 'Content-Type: application/json' \
  -d '{"id": "big-moe:3fa94c1e:7"}'
```

```json
{"id": "big-moe:3fa94c1e:7", "gpu": "0000:09:00.0", "mib": 8192}
```

| Status | Body | When |
|---|---|---|
| 200 | the engine's answer, id prefixed | The engine has the memory back |
| 400 | `{"error": "..."}` | The body is not a JSON object, or `id` is missing, empty, or not a string or number |
| 401 | | API keys are set and the call has none |
| 404 | `{"error": "..."}` | The id has no prefix of a lend model (an engine id without its prefix, or a JSON number), it has no run, its run is not the model's current run, or that model is not running. No engine gets the call. Also when the model stopped during the return |
| other | the engine's answer | For example 404 for a lease the engine does not know, 409 with `"retry": true` while the memory is in use, 501 or 503 |

A lease ends with the engine process that lent it. After the model is
unloaded, swapped out or restarted, a return of its old lease answers 404,
also once the model runs again: the new run has a new token.

## GET /vram/state

```sh
curl -s localhost:8080/vram/state
```

```json
{
  "gpus": [
    {"gpu": "0000:05:00.0", "lendable": 8112, "lent": 0, "model": "big-moe"},
    {"gpu": "0000:09:00.0", "lendable": 0, "lent": 8192, "model": "big-moe"}
  ],
  "leases": [
    {"id": "big-moe:3fa94c1e:7", "gpu": "0000:09:00.0", "mib": 8192, "model": "big-moe"}
  ]
}
```

The answer is always 200 (401 aside):

- `gpus`: one entry for each card of each ready lend model, with the address
  normalized to `0000:0a:00.0` and a `model` field. A card that two models
  use appears once for each.
- `leases`: every open lease, id prefixed as a lend answers it, with a
  `model` field.
- Both lists are empty when no lend model is ready.

llama-swap reads each engine in parallel, each read bounded by 35 s. An
engine that fails or times out is left out of the answer, and llama-swap logs
a warning.

## Timeouts

Forwarded lend and return calls have no llama-swap timeout. The engine's own
wait applies: the Maya and llama.cpp implementations answer 503 after 30 s
(see [api.md](api.md)).

## Other answers

- An unknown path under `/vram/` answers 404.
- A wrong method, for example `GET /vram/lend`, answers 405.

## Common failures

- **409 `{"max_mib": 0}` on every lend.** Nothing can serve the lend. The
  model is not loaded: llama-swap never loads one for a lend, so send an
  ordinary request to the model first (`curl -s localhost:8080/running`
  shows what is ready). The other causes: the model has no
  `vramLend: true`, the card is not one of the model's cards, or its
  `/vram/state` could not be read (see the llama-swap log).
- **409 with `max_mib` above 0.** A model holds the card but cannot lend that
  much now. Ask for `max_mib` or less, or wait.
- **404 on a return.** The model that lent the lease stopped, so the lease
  ended with it and the memory went back to the driver, also when the
  model has loaded again since or stopped during the return. Or the id lost its `<model id>:<run>:`
  prefix: send the id exactly as the lend answered it.
- **401.** `apiKeys` is set. Send the key as for `/running`.
- **A model is missing from `/vram/state`.** Its read failed or took more
  than 35 s. An engine must give lease ids in `/vram/state` as JSON strings:
  a number makes that model's state unreadable, so it is left out of the
  state and of the card lists. A lend answer may carry a number id.
- **The model loads when a borrower calls it.** The borrower went through
  llama-swap's `/upstream/<model>/` passthrough. That path is an ordinary
  request and loads the model. Use the `/vram` routes on llama-swap's own
  port.

## Berth and direct engine access

The llama-swap route is for borrowers that want one address. It does not
replace the engine's own routes: each engine still serves `/vram/lend`,
`/vram/return` and `/vram/state` on its own port, with its own lease ids.

A broker such as [Berth](https://github.com/T-Crypt/berth) keeps its own
`lend_url` for each holder and talks to each engine directly. That keeps
working beside llama-swap. The two kinds of id are not the same: the engine's
port knows the engine id only (`7`), llama-swap knows the prefixed id only
(`big-moe:3fa94c1e:7`). Return a lease with the id that the place it is
returned to knows.

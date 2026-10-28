# VRAM Lend through llama-swap

[llama-swap](https://github.com/mostlygeek/llama-swap) runs several inference
engines behind one port, assigns each engine its own port, and starts and
stops engines on demand. A borrower reaches an engine's VRAM Lend routes
through llama-swap's passthrough:

```
POST http://127.0.0.1:8080/upstream/<model>/vram/lend
POST http://127.0.0.1:8080/upstream/<model>/vram/return
GET  http://127.0.0.1:8080/upstream/<model>/vram/state
```

The bodies, answers and lease ids are the engine's, as specified in
[api.md](api.md). llama-swap adds nothing to the protocol. What it must not
do is start, swap or queue a model for such a call: a lend is for an engine
that is serving, and an engine that is not loaded has no memory to lend.
One config entry and one small llama-swap change give that.

## Config

Add `^/vram/` to `upstream.ignorePaths`. Listing the option replaces its
default, so repeat the static-asset pattern:

```yaml
upstream:
  ignorePaths:
    - '.*\.(js|json|css|png|gif|jpg|jpeg|ico|txt)$'
    - ^/vram/

models:
  big-moe:
    cmd: >
      /home/you/llama.cpp/build/bin/llama-server --port ${PORT}
      -m /home/you/models/big-moe.gguf
      --moe-cache-mib 8192 --vram-lend-max-mib 8192
```

The pattern is matched against the path after `/upstream/<model>` is
stripped, so it is `^/vram/`, not `^/upstream/.*/vram/`. `<model>` is the
model id or one of its aliases. No per-model option is needed: any engine
that serves the routes lends; an engine that does not answers its own 404 or
501.

## What a /vram call does

| model state | released llama-swap (v267) | with the fork branch |
|---|---|---|
| not loaded (stopped, starting, stopping) | 409, `path matches upstream.ignorePaths`; nothing starts | the same |
| loaded | forwarded, but through the scheduler: a concurrency slot (429 at `concurrencyLimit: 1` while a request streams), an in-flight count, a ttl reset | forwarded straight to the engine, outside the scheduler: no slot, no process in-flight count, no ttl reset |

The 409 on an unloaded model is released behaviour, from llama-swap's
`upstream.ignorePaths` (PR 869). The loaded-model row is the gap: on
released llama-swap a lend can be refused with 429 while the engine streams
an answer, which is the moment lending exists for, and a borrower that
polls `/vram/state` keeps the model loaded past its ttl.

The fork branch
[untracked-paths](https://github.com/TacoTakumi/llama-swap/tree/untracked-paths),
on mostlygeek/llama-swap 6c855da (v267), closes it with a general change:
a request matching `ignorePaths` on a loaded model is served the way an
ignored websocket already is. It is ten lines of logic, no new config key,
and it also stops an open ComfyUI tab's job polling from keeping
`comfyui_auto` resident. Measured with llama-swap's own stand-in model, no
GPU: a ttl 20 model under a 2 s poll of an ignored path stayed loaded at
+38 s on v267 and was unloaded at +38 s on the branch; a matched request
beside a streaming request at `concurrencyLimit: 1` answered 429 in 12 ms on
v267 and 200 in 16 ms on the branch. Upstream issue:
[1135](https://github.com/mostlygeek/llama-swap/issues/1135) asks for the
same change; the maintainer's answer there prefers a `/metrics` extension
for the scrape case, which does not cover a control call. Upstream PR: not
opened yet.

## Borrowers

**A broker that starts the engine**, such as
[Berth](https://github.com/T-Crypt/berth), knows which run holds which card
and asks that run's `lend_url`. With llama-swap in front, the entry's
`lend_url` is `http://127.0.0.1:8080/upstream/<model>`: it does not move
when llama-swap assigns a new port, and llama-swap's API key applies
(`Authorization: Bearer <key>`). Berth asks only a holder it started, so the
409 for an unloaded model, which carries a text body rather than the
protocol's `{"max_mib": N}`, never reaches it.

**A borrower that knows the card and not the model** finds the model in two
or three calls: `GET /running` lists the ready models; `GET
/upstream/<m>/vram/state` of each candidate lists the cards it holds, with
`lendable` and `lent` per card; the borrower picks the model that lists the
card. Two models that share a card both list it; a 409 with `max_mib` from
the first moves the borrower to the next. A 409 without `max_mib` means the
model is not loaded.

**A borrower on the engine's own port** keeps working. Each engine still
serves `/vram/lend`, `/vram/return` and `/vram/state` there, with the same
ids, so a lease taken through llama-swap can be returned on the engine's
port and the other way round. A fixed port in the engine's `cmd` (and a
`proxy:` line) is the alternative to the passthrough when a borrower cannot
use a path prefix.

## Status codes

llama-swap adds three answers; everything else is the engine's, unchanged:

| Status | When |
|---|---|
| 401 | `apiKeys` is set and the call has no key |
| 404 `model not found` | `<model>` is not a model id or alias |
| 409 `model <m> is not loaded; path matches upstream.ignorePaths` | the model is not ready; nothing was started |

Leases end with the engine process. After an unload, a swap or a restart,
a return of an old lease reaches the new engine, which answers its own 404
(no open lease); a broker counts that as returned. The engine's own wait
applies to a lend (30 s in the Maya and llama.cpp implementations), and its
503 is passed through.

## Common failures

- **The model loads when a borrower calls it.** `^/vram/` is not in
  `upstream.ignorePaths`, or the list was written without the default
  static-asset pattern and only that part was noticed. Listing the option
  replaces the default.
- **409 on every call.** The model is not loaded. llama-swap never loads a
  model for a `/vram` call: send an ordinary request to the model first
  (`curl -s localhost:8080/running` shows what is ready).
- **429 `Too many requests` on a lend while a request streams.** Released
  llama-swap with a low `concurrencyLimit`. Raise the limit for that model,
  or run the fork branch.
- **The model never unloads while a client polls `/vram/state`.** Released
  llama-swap: every forwarded request resets the ttl. The fork branch.
- **404 on a return.** The engine process that lent the lease has ended
  (unload, swap, restart), or the id is wrong. See [api.md](api.md).
- **The address form is refused.** The engine, not llama-swap, parses
  `gpu`; see [api.md](api.md) for the accepted forms.

## History

The first llama-swap design (fork branch `vram-route`, on mostlygeek 783c323,
later rebased as `vram-route-v267`) served `/vram/lend`, `/vram/return` and
`/vram/state` on llama-swap's own port, routed each call by card to the
ready models that had `vramLend: true`, and prefixed lease ids with the
model id and a per-run token. It worked and was unit-tested, but it was 33
files and 4667 lines for one route, three times the size of upstream's
capacity router, and most of it did in llama-swap what a borrower does in
two calls. It was replaced on 2026-10-28 by the passthrough and the general
change above. The branch stays on the fork as history.

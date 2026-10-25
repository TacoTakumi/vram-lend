# VRAM Lend engine protocol

This page specifies the control lines between the inference server and the
engine process. The server sends one command per line on the engine's input
and reads the answer lines from the engine's output. The HTTP API that sits on
top of these lines is in [api.md](api.md).

The engine owns the leases. The server only translates and keeps a record of
the leases it handed out.

## Line rules

- One command per line. The command words are case-sensitive.
- Extra blanks and a trailing CR are ignored.
- Answers carry no tag. The server matches them by order (see
  [Answer order](#answer-order)).
- A command that does not parse gets an `ERR MALFORMED` answer. The engine
  never passes a malformed lend command on to its other command handling.
- `<dev>` is the engine's own device number. The engine reports which PCI
  address each device number has on its other output lines (the Maya engines
  print a `VRAM dev=<n> pci=<address> ...` line per GPU at load). The server
  uses these lines to map the `gpu` field of the HTTP API to `<dev>`.

## LEND

```
LEND <dev> <mib>
```

Lend `<mib>` MiB of device `<dev>`, rounded up to whole blocks of that device.

| Answer | Meaning |
|---|---|
| `LENT <id> <dev> <mib>` | The lease. `<mib>` is the size actually lent |
| `ERR MAX <mib>` | More than the device can lend now. `<mib>` is its lendable |
| `ERR NODEV <dev>` | `<dev>` is well formed but is not a device that lends |
| `ERR MALFORMED LEND <dev> <mib>` | `<dev>` is not a non-negative integer, or `<mib>` is missing, 0, signed, not a whole number or too large, or the line has an extra word |

The engine checks the request against the device's lendable before it rounds
up, so a lend never takes more than lendable. The device's memory is released
to the driver before the `LENT` line is written.

## RETURN

```
RETURN <id>
```

Give the memory of lease `<id>` back to the engine's cache.

| Answer | Meaning |
|---|---|
| `RETURNED <id>` | The memory is back in the cache |
| `ERR BUSY <id>` | Another process still holds the memory. Nothing changed. The lease stays open |
| `ERR UNKNOWN <id>` | No open lease has this id |
| `ERR MALFORMED RETURN <id>` | No id, or an extra word |

## LENDABLE

```
LENDABLE
```

Report each device that can lend. The answer is one line per device, in a
fixed order, then `END`:

```
LENDABLE dev=0 lendable=0 lent=0
LENDABLE dev=3 lendable=2128 lent=8192
END
```

- `lendable`: MiB the device can lend now, a multiple of its block size.
- `lent`: MiB the open leases hold on that device.

A device with nothing lendable now is still listed. `ERR MALFORMED LENDABLE`
answers a line with a word after `LENDABLE`.

## Lease ids

- An id is opaque to the server. The Maya engines use decimal numbers, never
  0, from 1 again in each process.
- An id is unique for the life of the engine process. It is never handed out
  twice, also after its lease came back.
- The engine does not know who borrows. A lease is an id, a device and a size.
- The server makes ids unique across engine restarts: it adds the highest id
  that earlier engine processes handed out. The engine does not need to do
  this.

## When the engine answers

The commands come while requests run. The engine's input reader takes each
lend command off the input and puts it in one queue. The engine's main thread
answers the queue in order:

- `LENDABLE` only reads figures. The engine answers it at any point, also
  between two tokens of a request.
- `LEND` and `RETURN` change the cache. The engine answers them only where
  nothing is in flight on any device:
  - at once when the engine is idle;
  - between two scheduler steps, after every issued token has completed;
  - while a prompt is read, between two prompt-path calls.
- A `LENDABLE` behind a `LEND` or `RETURN` in the queue waits for it, so the
  answers go out in the order the commands came.

A single prompt-path call can take seconds on a long prompt. A lend that
arrives during such a call is answered when the call ends. In the Maya
implementation this was measured at 9 s (GLM-5.3-Flash) and 13.8 s
(DeepSeek-V4.1) for long prompts.

## Answer order

The engine answers every lend command, in the order the commands came, from
one thread. Because answers carry no tag, the server matches each answer to
the oldest command that has no answer yet. The server keeps that rule also for
a command whose HTTP caller stopped waiting: its answer is still its own and
is never given to the next command.

An answer ends at its first line, except a `LENDABLE` answer, which ends at
`END`.

Lend answers come on the same output as the engine's request output. The
server tells them apart by their first words (`LENT `, `RETURNED `,
`LENDABLE `, `END`, `ERR MAX`, `ERR NODEV`, `ERR BUSY`, `ERR UNKNOWN`,
`ERR MALFORMED LEND|RETURN|LENDABLE`). An engine's own error lines must not
start with these words.

## Late leases

The server waits a fixed time for each answer (30 s in the Maya
implementation). When a `LEND` is answered after that time:

1. The HTTP call has already answered 503.
2. The `LENT` answer still comes, and the server records the lease.
3. The server sends `RETURN <id>` for it by itself.
4. `/vram/state` lists the lease until `RETURNED` comes.
5. If the answer is `ERR BUSY`, the server sends the `RETURN` again at the
   next API call.

No lease is left open without an owner.

## Engine without lending

An engine that does not know the commands answers with its usual error for an
unknown command. The Maya engines answer
`ERR unsupported command '<word>'`. The server then answers 501 to lend and
return calls, and `/vram/state` shows every GPU with 0 and 0.

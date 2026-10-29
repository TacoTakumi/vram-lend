# Changelog

All notable changes to the vram-lend package are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The single source of truth for the version is `__version__` in
`src/vram_lend/__init__.py`; `vram-lend --version` and the build metadata both
derive from it. Release tags are of the form `vX.Y.Z`. The engine forks, the
llama-swap branch and the protocol documents in this repository are not
versioned by this file; the README status table tracks them.

## [0.1.1]

### Added
- `vram-lend --version`.

### Changed
- The version lives in `__version__` only; `pyproject.toml` reads it at build
  time instead of carrying its own copy.

## [0.1.0]

First release of the borrower side: a command and an agent skill for the VRAM
Lend protocol specified in `docs/api.md` and `docs/llama-swap.md`.

### Added
- **The `vram-lend` command** (`src/vram_lend/lend.py`, standard library
  only, runs on its own as one file). It finds the lend endpoints behind the
  servers it is given (`--server`, `VRAM_LEND_SERVER`, default
  `http://127.0.0.1:8080`): an engine that answers `/vram/state`, or a
  llama-swap whose `/running` lists ready models, each reached through
  `/upstream/<model>`. It never calls a model llama-swap does not list as
  ready, never probes `/running` after a 409, and so never loads a model.
- **`vram-lend state`**: every endpoint with each GPU's lendable and lent MiB,
  the open leases, and a note for every server that resolved to nothing.
  `--gpu` filters one card, `--json` prints the same as JSON.
- **`vram-lend lend --gpu <pci> --mib N`**: lends on the first endpoint that
  lists the card with enough lendable and prints the lease as JSON (id, gpu,
  mib, endpoint). PCI addresses are accepted in the CUDA, NVML and lspci
  forms.
- **`vram-lend return --id <id> [--endpoint URL]`**: returns a lease. Lease
  ids are per engine, so `--endpoint` names the one the lend printed. A 404
  everywhere, a model that is not loaded, or an engine that cannot lend all
  count as the lease ended with its engine process (exit 0); a server that
  gave no usable answer makes a not-found return exit 3 instead.
- **`vram-lend run --gpu <pci> --mib N <command>`**: lends, starts the
  command with `VRAM_LEND_LEASE`, `VRAM_LEND_MIB`, `VRAM_LEND_GPU` and
  `VRAM_LEND_ENDPOINT` in its environment, and returns the lease however the
  command ends: normal exit, failure, SIGINT or SIGTERM. The first signal goes
  to the command, a second one kills it with SIGKILL, and a signal while the
  lend or the return is in flight prints what is in flight and that the lease
  will be returned. It exits with the command's code.
- **The refusal map**: every status in the protocol (400, 401, 404, 409 with
  and without `max_mib`, 429 with bounded backoff, 501, 502, 503) is one line
  on stderr with the cause and the next step. Exit 1 means nothing was lent,
  2 wrong arguments, 3 a lease may still be open.
- **`VRAM_LEND_API_KEY`**: sent as a bearer token on every call for a
  llama-swap with `apiKeys`.
- **The `vram-lend` skill** (`src/vram_lend/skills/vram-lend/SKILL.md`),
  shipped as package data and installed into Claude Code, pi, Hermes or
  opencode by `vram-lend skills install` (AgentSquire). It tells an agent
  when to lend, the steps, what each refusal means, the rules it must keep,
  that ComfyUI under llama-swap is not a case for it, and the three calls per
  shape by hand.
- **`bench/alloc-cuda.py`**: a stand-in borrower (CUDA runtime through
  ctypes) for an end-to-end check of the command on a serving engine.
- **README section "Agent instructions"** and a status row for the command
  and the skill.

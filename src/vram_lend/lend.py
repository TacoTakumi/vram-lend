"""vram-lend: lend yourself GPU memory from a serving inference engine.

This file imports only the standard library, so it runs on its own:
``python3 lend.py state``. The package installs it as ``vram-lend``.

The verbs mirror the VRAM Lend HTTP API (docs/api.md): ``state``, ``lend``,
``return``, and ``run``, which lends, runs a command and returns the lease.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_SERVER = "http://127.0.0.1:8080"
PROBE_TIMEOUT = 5.0
LEND_TIMEOUT = 40.0
# Waits between retries of a 429 (llama-swap's scheduler is busy) or of a
# return that answers 409 retry (the memory is still in use). Bounded on
# purpose: a lend that cannot go through should fail, not hang.
BACKOFF = [0.2, 0.4, 0.8, 1.6, 3.2]

EXIT_OK = 0
EXIT_NO_MEMORY = 1      # nothing was lent; the job must not start
EXIT_USAGE = 2
EXIT_LEASE_OPEN = 3     # a lease may still be open; return it again

_PCI = re.compile(r"^(?:([0-9a-f]{1,8}):)?([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vram-lend",
        description="Lend yourself GPU memory from a serving inference engine, "
                    "directly or through llama-swap, and give it back.",
    )
    parser.add_argument(
        "--server", action="append", metavar="URL",
        help="an engine or llama-swap base URL; repeatable. Default: "
             "VRAM_LEND_SERVER (comma separated) or " + DEFAULT_SERVER,
    )
    sub = parser.add_subparsers(dest="verb", metavar="verb")

    p_state = sub.add_parser("state", help="list every lend endpoint found and what each GPU can lend")
    p_state.add_argument("--gpu", metavar="PCI", help="show only this GPU")
    p_state.add_argument("--json", action="store_true", help="print JSON")

    p_lend = sub.add_parser("lend", help="lend memory on one GPU and print the lease; return it later")
    p_lend.add_argument("--gpu", required=True, metavar="PCI", help="PCI address of the GPU")
    p_lend.add_argument("--mib", required=True, type=int, metavar="N", help="MiB to lend")

    p_return = sub.add_parser("return", help="return a lease by id")
    p_return.add_argument("--id", required=True, metavar="ID", help="lease id from lend")
    p_return.add_argument("--endpoint", metavar="URL",
                          help="the endpoint lend printed with the lease; return there only")

    p_run = sub.add_parser("run", help="lend, run a command, return the lease when it exits")
    p_run.add_argument("--gpu", required=True, metavar="PCI", help="PCI address of the GPU")
    p_run.add_argument("--mib", required=True, type=int, metavar="N", help="MiB to lend")
    p_run.add_argument("command", nargs=argparse.REMAINDER, metavar="-- command",
                       help="the command to run")

    sub.add_parser("skills", help="install the vram-lend skill into agent harnesses "
                                  "(needs the pip package)")
    return parser


class LendError(Exception):
    """A failure the command reports as one line and a non-zero exit."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def normalize_pci(text: str) -> str | None:
    """The server's answer form: lower case, 4-digit domain, bus:dev.fn.

    Accepts CUDA's ``0000:0A:00.0``, NVML's ``00000000:0a:00.0`` and lspci's
    ``0a:00.0``. Returns None when the text is not a PCI address.
    """
    m = _PCI.match(text.strip().lower())
    if not m:
        return None
    domain = (m.group(1) or "0")[-4:].rjust(4, "0")
    return f"{domain}:{m.group(2)}:{m.group(3)}.{m.group(4)}"


def servers_from(given: list[str] | None, env: dict | None = None) -> list[str]:
    """--server values, else VRAM_LEND_SERVER (comma separated), else the default."""
    env = os.environ if env is None else env
    raw = list(given or [])
    if not raw:
        raw = [s for s in env.get("VRAM_LEND_SERVER", "").split(",")]
    raw = [s.strip().rstrip("/") for s in raw if s.strip()]
    return raw or [DEFAULT_SERVER]


class Http:
    """JSON over HTTP with an optional bearer key; never raises on a status."""

    def __init__(self, key: str | None = None):
        self.key = key

    def call(self, method: str, url: str, body: dict | None = None,
             timeout: float = PROBE_TIMEOUT):
        """Returns (status, parsed body or text). status 0 means no answer."""
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.status, _parse(r.read())
        except HTTPError as e:
            return e.code, _parse(e.read())
        except (URLError, OSError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            return 0, f"no answer: {reason}"


def _parse(raw: bytes):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()


class Endpoint:
    """One place that serves /vram/state, /vram/lend and /vram/return."""

    def __init__(self, url: str, label: str, http: Http):
        self.url = url
        self.label = label
        self.http = http

    def state(self):
        return self.http.call("GET", self.url + "/vram/state")

    def lend(self, gpu: str, mib: int):
        return self.http.call("POST", self.url + "/vram/lend",
                              {"gpu": gpu, "mib": mib}, timeout=LEND_TIMEOUT)

    def give_back(self, lease_id: str):
        return self.http.call("POST", self.url + "/vram/return",
                              {"id": lease_id}, timeout=LEND_TIMEOUT)


class Resolved:
    """What the servers resolved to. Unpacks as (endpoints, notes)."""

    def __init__(self):
        self.endpoints: list[Endpoint] = []
        self.notes: list[str] = []
        # Servers that gave neither an endpoint nor a definite answer that no
        # lease can live there (no answer, 401, 429, 5xx, anything unexpected).
        self.uncertain: list[str] = []

    def __iter__(self):
        return iter((self.endpoints, self.notes))


def resolve(servers: list[str], http: Http) -> Resolved:
    """Each server is an engine, a llama-swap, or nothing (with the reason).

    An engine answers GET /vram/state with 200 and a gpus list. llama-swap
    answers GET /running with a running list; each model in state ready is
    one endpoint at /upstream/<model>. A model llama-swap does not list as
    ready is never called, so a call here can never load a model.
    """
    out = Resolved()
    endpoints, notes = out.endpoints, out.notes
    for server in servers:
        status, body = http.call("GET", server + "/vram/state")
        if status == 200 and isinstance(body, dict) and isinstance(body.get("gpus"), list):
            endpoints.append(Endpoint(server, server, http))
            continue
        if status == 401:
            notes.append(f"{server}: 401 on /vram/state; set VRAM_LEND_API_KEY")
            out.uncertain.append(server)
            continue
        if status == 0:
            notes.append(f"{server}: {body}")
            out.uncertain.append(server)
            continue
        if status == 501:
            notes.append(f"{server}: the engine cannot lend (501 on /vram/state)")
            continue
        if status == 409:
            # llama-swap's passthrough for a model that is not loaded. Nothing
            # else is probed on this server: a GET of <server>/running would
            # be /upstream/<model>/running, which loads the model.
            notes.append(f"{server}: the model is not loaded (409 on /vram/state); "
                         "llama-swap started nothing")
            continue
        if status != 404:
            notes.append(f"{server}: {status} on /vram/state: {_short(body)}")
            out.uncertain.append(server)
            continue
        # Only a 404 on /vram/state can be a plain llama-swap port.
        r_status, r_body = http.call("GET", server + "/running")
        if r_status == 200 and isinstance(r_body, dict) and isinstance(r_body.get("running"), list):
            ready = [m for m in r_body["running"]
                     if isinstance(m, dict) and m.get("state") == "ready" and m.get("model")]
            if not ready:
                notes.append(f"{server}: llama-swap, no model ready; "
                             "nothing lends until a model is loaded")
            for m in ready:
                name = str(m["model"])
                endpoints.append(Endpoint(f"{server}/upstream/{name}", f"{server} model {name}", http))
            continue
        if r_status == 401:
            notes.append(f"{server}: 401 on /running; set VRAM_LEND_API_KEY")
            out.uncertain.append(server)
            continue
        notes.append(f"{server}: no VRAM Lend API ({status} on /vram/state, "
                     f"{r_status} on /running); is this the engine or llama-swap port?")
        if r_status == 0 or r_status == 429 or r_status >= 500:
            # /running gave no usable answer: this may still be a llama-swap
            # with a loaded model that holds a lease.
            out.uncertain.append(server)
    return out


def gpus_of(endpoint: Endpoint) -> tuple[list[dict], list[dict], str | None]:
    """(gpus, leases, problem) from one endpoint's /vram/state."""
    status, body = endpoint.state()
    if status == 200 and isinstance(body, dict) and isinstance(body.get("gpus"), list):
        gpus = []
        for g in body["gpus"]:
            if isinstance(g, dict) and isinstance(g.get("gpu"), str):
                pci = normalize_pci(g["gpu"]) or g["gpu"]
                gpus.append({"gpu": pci, "lendable": int(g.get("lendable", 0)),
                             "lent": int(g.get("lent", 0))})
        leases = body.get("leases") if isinstance(body.get("leases"), list) else []
        return gpus, leases, None
    if status == 409 and not (isinstance(body, dict) and "max_mib" in body):
        return [], [], "not loaded (409); llama-swap started nothing"
    if status == 501:
        return [], [], "the engine cannot lend (501)"
    if status == 404:
        return [], [], "no /vram routes (404); the engine does not lend"
    if status == 0:
        return [], [], str(body)
    return [], [], f"{status} on /vram/state: {_short(body)}"


def _short(body) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body)
    return str(body)[:120]


def cmd_state(args, http: Http) -> int:
    servers = servers_from(args.server)
    endpoints, notes = resolve(servers, http)
    want = normalize_pci(args.gpu) if args.gpu else None
    if args.gpu and want is None:
        raise LendError(f"not a PCI address: {args.gpu}", EXIT_USAGE)
    report = []
    for ep in endpoints:
        gpus, leases, problem = gpus_of(ep)
        if want:
            gpus = [g for g in gpus if g["gpu"] == want]
            leases = [l for l in leases if normalize_pci(str(l.get("gpu", ""))) == want]
        report.append({"endpoint": ep.url, "label": ep.label, "gpus": gpus,
                       "leases": leases, "problem": problem})
    if args.json:
        print(json.dumps({"servers": servers, "endpoints": report, "notes": notes}, indent=2))
    else:
        for r in report:
            print(f"{r['label']}")
            if r["problem"]:
                print(f"  {r['problem']}")
            for g in r["gpus"]:
                print(f"  {g['gpu']}  lendable {g['lendable']} MiB  lent {g['lent']} MiB")
            for l in r["leases"]:
                print(f"  lease {l.get('id')}  {l.get('gpu')}  {l.get('mib')} MiB")
            if not r["problem"] and not r["gpus"]:
                print("  no GPU listed" + (f" for {want}" if want else ""))
        for n in notes:
            print(f"note: {n}")
    return 0 if endpoints else 1


def _describe(status: int, body, label: str, gpu: str | None = None) -> str:
    """One line for a status the docs list, with the next step when it helps."""
    if status == 0:
        return f"no answer from {label}: {body}"
    if status == 400:
        return f"400 from {label}: {_short(body)}"
    if status == 401:
        return f"401 from {label}: set VRAM_LEND_API_KEY"
    if status == 404:
        return f"404 from {label}: no /vram routes; the engine does not lend"
    if status == 409 and isinstance(body, dict) and "max_mib" in body:
        return f"409 from {label}: it can lend at most {body['max_mib']} MiB on {gpu}"
    if status == 409:
        return f"409 from {label}: the model is not loaded; llama-swap started nothing"
    if status == 429:
        return (f"429 from {label}: too many requests; released llama-swap with a low "
                "concurrencyLimit, retry when the request ends")
    if status == 501:
        return f"501 from {label}: the engine cannot lend"
    if status == 502:
        return f"502 from {label}: the engine gave an answer the server does not know"
    if status == 503:
        return f"503 from {label}: the engine did not answer in time; do not use the memory"
    return f"{status} from {label}: {_short(body)}"


def _with_retry(call, retry_on=(429,), retry_flag=False):
    """Call until the status is not in retry_on (or not a 409 with retry)."""
    waits = list(BACKOFF)
    while True:
        status, body = call()
        again = status in retry_on or (
            retry_flag and status == 409 and isinstance(body, dict) and body.get("retry"))
        if not again or not waits:
            return status, body
        time.sleep(waits.pop(0))


def lend_on(endpoints: list[Endpoint], gpu: str, mib: int) -> tuple[dict, Endpoint]:
    """The first endpoint that lists the card with enough lendable lends.

    A 409 with max_mib or too little lendable moves on to the next endpoint.
    A 409 without max_mib (not loaded), 501, 404 and 502 skip the endpoint.
    A 503 stops at once: the memory must not be used. Raises LendError with
    every reason when nothing lent.
    """
    reasons: list[str] = []
    listed = False
    for ep in endpoints:
        gpus, _leases, problem = gpus_of(ep)
        if problem:
            reasons.append(f"{ep.label}: {problem}")
            continue
        card = next((g for g in gpus if g["gpu"] == gpu), None)
        if card is None:
            continue
        listed = True
        if card["lendable"] < mib:
            reasons.append(f"{ep.label}: it can lend at most {card['lendable']} MiB on {gpu}")
            continue
        status, body = _with_retry(lambda: ep.lend(gpu, mib))
        if status == 200 and isinstance(body, dict) and "id" in body:
            lease = {"id": str(body["id"]), "gpu": normalize_pci(str(body.get("gpu", gpu))) or gpu,
                     "mib": int(body.get("mib", mib)), "endpoint": ep.url}
            return lease, ep
        if status == 503:
            raise LendError(_describe(status, body, ep.label, gpu), EXIT_NO_MEMORY)
        if status in (400, 401):
            raise LendError(_describe(status, body, ep.label, gpu), EXIT_USAGE)
        reasons.append(_describe(status, body, ep.label, gpu))
    if not endpoints:
        raise LendError(f"no lend endpoint found; nothing lends on {gpu}", EXIT_NO_MEMORY)
    if not listed and not reasons:
        raise LendError(f"no endpoint lists GPU {gpu}; check vram-lend state", EXIT_NO_MEMORY)
    raise LendError(f"nothing lent on {gpu}: " + "; ".join(reasons), EXIT_NO_MEMORY)


def return_lease(endpoints: list[Endpoint], lease_id: str) -> tuple[int, str]:
    """Return a lease by id on the endpoint that knows it.

    Every endpoint that answers 404 does not have it. When every one answers
    404, or none is loaded, the lease ended with its engine process and counts
    as returned. Returns (exit code, message).
    """
    unreachable: list[str] = []
    for ep in endpoints:
        status, body = _with_retry(lambda: ep.give_back(lease_id), retry_flag=True)
        if status == 200:
            return EXIT_OK, f"returned lease {lease_id} at {ep.label}"
        if status == 404:
            continue
        if status == 409:
            if isinstance(body, dict) and body.get("retry"):
                return EXIT_LEASE_OPEN, (f"lease {lease_id} at {ep.label} is still in use "
                                         "(409 retry); free the memory and return it again")
            # llama-swap's text 409 (not loaded), or a 409 without retry: the
            # engine process that lent the lease has ended.
            continue
        if status in (501,):
            continue
        if status in (0, 429, 502, 503):
            unreachable.append(_describe(status, body, ep.label))
            continue
        return EXIT_LEASE_OPEN, f"lease {lease_id}: {_describe(status, body, ep.label)}"
    if unreachable:
        return EXIT_LEASE_OPEN, (f"lease {lease_id} not returned: " + "; ".join(unreachable)
                                 + "; return it again")
    return EXIT_OK, (f"lease {lease_id} is already gone: no open lease has this id; "
                     "a lease ends with the engine process")


def _gpu_arg(text: str) -> str:
    gpu = normalize_pci(text)
    if gpu is None:
        raise LendError(f"not a PCI address: {text} (use a form like 0000:0a:00.0)", EXIT_USAGE)
    return gpu


def _mib_arg(mib: int) -> int:
    if mib <= 0:
        raise LendError("--mib must be a whole number above 0", EXIT_USAGE)
    return mib


def cmd_lend(args, http: Http) -> int:
    gpu, mib = _gpu_arg(args.gpu), _mib_arg(args.mib)
    endpoints, notes = resolve(servers_from(args.server), http)
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    lease, _ep = lend_on(endpoints, gpu, mib)
    print(json.dumps(lease))
    return EXIT_OK


def cmd_return(args, http: Http) -> int:
    lease_id = str(args.id)
    if args.endpoint:
        # The endpoint the lend printed: ids are per engine, so this is the
        # one place the lease can be. Probe /vram/state first (it loads
        # nothing): a 404 there is a URL without the routes, whose 404 on a
        # return would say nothing about the lease.
        url = args.endpoint.rstrip("/")
        ep = Endpoint(url, url, http)
        status, body = ep.state()
        if status == 200 and isinstance(body, dict) and isinstance(body.get("gpus"), list):
            code, message = return_lease([ep], lease_id)
        elif status == 409 and not (isinstance(body, dict) and "max_mib" in body):
            code, message = EXIT_OK, (f"lease {lease_id} is already gone: the model at {url} "
                                      "is not loaded; a lease ends with the engine process")
        elif status == 501:
            code, message = EXIT_OK, (f"lease {lease_id} is already gone: the engine at {url} "
                                      "cannot lend (501), so no lease lives there")
        elif status == 404:
            code, message = EXIT_LEASE_OPEN, (f"{url} is not a lend endpoint (404 on /vram/state); "
                                              "pass the endpoint the lend printed")
        else:
            code, message = EXIT_LEASE_OPEN, (f"lease {lease_id} not returned: "
                                              f"{_describe(status, body, url)}; return it again")
    else:
        resolved = resolve(servers_from(args.server), http)
        for n in resolved.notes:
            print(f"note: {n}", file=sys.stderr)
        code, message = return_lease(resolved.endpoints, lease_id)
        if code == EXIT_OK and resolved.uncertain and "already gone" in message:
            # Every endpoint said no such lease, but a server that gave no
            # usable answer may still hold it.
            code, message = EXIT_LEASE_OPEN, (f"lease {lease_id} not returned: no usable answer "
                                              "from " + ", ".join(resolved.uncertain)
                                              + "; return it again")
    print(message, file=sys.stderr if code else sys.stdout)
    return code


def cmd_run(args, http: Http) -> int:
    """Lend, run the command, and return the lease however the command ends.

    SIGINT and SIGTERM are caught from before the lend until after the
    return, so neither can leave a lease open: the first one goes to the
    command, a second one kills it with SIGKILL, and one that arrives before
    the command starts stops the run without starting it.
    """
    gpu, mib = _gpu_arg(args.gpu), _mib_arg(args.mib)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise LendError("run needs a command after --", EXIT_USAGE)
    endpoints, notes = resolve(servers_from(args.server), http)
    for n in notes:
        print(f"note: {n}", file=sys.stderr)

    proc = None
    signals: list[int] = []
    previous = {}

    def forward(signum, _frame):
        signals.append(signum)
        if proc is None or proc.poll() is not None:
            # No live command: the lend or the return is in flight.
            if proc is None:
                what = ("the lend is still in flight; the command will not start "
                        "and the lease will be returned")
            else:
                what = "the command has ended and the return is in flight"
            if len(signals) == 1:
                print(f"vram-lend: signal noted; {what}", file=sys.stderr)
            else:
                print(f"vram-lend: {what}; do not kill vram-lend, that would leave "
                      "the lease open", file=sys.stderr)
            return
        if len(signals) >= 2:
            proc.kill()
        else:
            proc.send_signal(signum)

    def restore():
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, forward)
    try:
        lease, ep = lend_on(endpoints, gpu, mib)
    except BaseException:
        restore()
        raise

    code = EXIT_NO_MEMORY
    try:
        print(f"vram-lend: lent {lease['mib']} MiB on {lease['gpu']} "
              f"(lease {lease['id']} at {ep.label})", file=sys.stderr)
        env = dict(os.environ)
        env["VRAM_LEND_LEASE"] = lease["id"]
        env["VRAM_LEND_MIB"] = str(lease["mib"])
        env["VRAM_LEND_GPU"] = lease["gpu"]
        env["VRAM_LEND_ENDPOINT"] = ep.url
        if signals:
            print("vram-lend: interrupted before the command started", file=sys.stderr)
            code = 128 + signals[0]
        else:
            try:
                proc = subprocess.Popen(command, env=env)
            except OSError as e:
                print(f"vram-lend: cannot start {command[0]}: {e}", file=sys.stderr)
                code = 127
            except KeyboardInterrupt:
                code = 130
            else:
                while True:
                    try:
                        code = proc.wait()
                        break
                    except KeyboardInterrupt:
                        forward(signal.SIGINT, None)
                if code < 0:
                    code = 128 - code
    finally:
        try:
            back, message = return_lease([ep], lease["id"])
        finally:
            restore()
        print(f"vram-lend: {message}", file=sys.stderr)
        if back != EXIT_OK and code == 0:
            code = back
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb is None:
        parser.print_help()
        return 2
    if args.verb == "skills":
        print("vram-lend skills needs the pip package: pipx install vram-lend",
              file=sys.stderr)
        return 2
    http = Http(os.environ.get("VRAM_LEND_API_KEY") or None)
    try:
        if args.verb == "state":
            return cmd_state(args, http)
        if args.verb == "lend":
            return cmd_lend(args, http)
        if args.verb == "return":
            return cmd_return(args, http)
        if args.verb == "run":
            return cmd_run(args, http)
        parser.error(f"unknown verb {args.verb}")
        return EXIT_USAGE
    except LendError as e:
        print(f"vram-lend: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

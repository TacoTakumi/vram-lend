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
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_SERVER = "http://127.0.0.1:8080"
PROBE_TIMEOUT = 5.0
LEND_TIMEOUT = 40.0

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


def resolve(servers: list[str], http: Http) -> tuple[list[Endpoint], list[str]]:
    """Each server is an engine, a llama-swap, or nothing (with the reason).

    An engine answers GET /vram/state with 200 and a gpus list. llama-swap
    answers GET /running with a running list; each model in state ready is
    one endpoint at /upstream/<model>. A model llama-swap does not list as
    ready is never called, so a call here can never load a model.
    """
    endpoints: list[Endpoint] = []
    notes: list[str] = []
    for server in servers:
        status, body = http.call("GET", server + "/vram/state")
        if status == 200 and isinstance(body, dict) and isinstance(body.get("gpus"), list):
            endpoints.append(Endpoint(server, server, http))
            continue
        if status == 401:
            notes.append(f"{server}: 401 on /vram/state; set VRAM_LEND_API_KEY")
            continue
        if status == 0:
            notes.append(f"{server}: {body}")
            continue
        if status == 501:
            notes.append(f"{server}: the engine cannot lend (501 on /vram/state)")
            continue
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
            continue
        notes.append(f"{server}: no VRAM Lend API ({status} on /vram/state, "
                     f"{r_status} on /running); is this the engine or llama-swap port?")
    return endpoints, notes


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
        raise LendError(f"not a PCI address: {args.gpu}", 2)
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
        print(f"{args.verb}: not implemented yet", file=sys.stderr)
        return 2
    except LendError as e:
        print(f"vram-lend: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())

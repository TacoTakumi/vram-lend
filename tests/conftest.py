"""Stand-in servers for the tests.

``FakeEngine`` plays an inference engine that serves the VRAM Lend routes with
the protocol's rules: lends rounded up to whole blocks, leases by id, a return
of an unknown id answers 404. ``FakeSwap`` plays llama-swap in front of several
engines: ``/running`` lists the models with a state, and
``/upstream/<model>/vram/*`` is forwarded only for a loaded model. Both record
every request and take scripted answers that override a route for the next
calls, so a test can force any status the docs list.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest


def canonical(pci: str) -> str:
    """The server's answer form: lower case, 4-digit domain, function."""
    s = pci.strip().lower()
    parts = s.split(":")
    if len(parts) == 2:
        parts = ["0000"] + parts
    domain = parts[0][-4:].rjust(4, "0")
    return ":".join([domain] + parts[1:])


class Scripted:
    """Scripted answers per (method, path): a list consumed in order."""

    def __init__(self):
        self.scripts: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.requests: list[dict] = []
        self.require_key: str | None = None

    def script(self, method: str, path: str, *answers: tuple[int, object]) -> None:
        self.scripts.setdefault((method, path), []).extend(answers)

    def scripted(self, method: str, path: str):
        q = self.scripts.get((method, path))
        if q:
            return q.pop(0)
        return None


class FakeEngine(Scripted):
    def __init__(self, gpus: dict[str, int] | None = None, block: int = 64,
                 can_lend: bool = True):
        super().__init__()
        self.gpus = {canonical(k): v for k, v in (gpus or {}).items()}
        self.block = block
        self.can_lend = can_lend
        self.leases: dict[str, dict] = {}
        self.next_id = 1
        self.lock = threading.Lock()

    def state(self):
        lent = {g: 0 for g in self.gpus}
        for lease in self.leases.values():
            lent[lease["gpu"]] += lease["mib"]
        return {
            "gpus": [{"gpu": g, "lendable": self.gpus[g] - lent[g], "lent": lent[g]}
                     for g in self.gpus],
            "leases": list(self.leases.values()),
        }

    def handle(self, method: str, path: str, body: object):
        hit = self.scripted(method, path)
        if hit is not None:
            return hit
        if not self.can_lend:
            if path.startswith("/vram/"):
                return 501, {"error": "the engine cannot lend"}
            return 404, {"error": "not found"}
        if method == "GET" and path == "/vram/state":
            return 200, self.state()
        if method == "POST" and path == "/vram/lend":
            if not isinstance(body, dict) or not isinstance(body.get("mib"), int) \
                    or isinstance(body.get("mib"), bool) or body["mib"] <= 0:
                return 400, {"error": "mib must be a whole number above 0"}
            gpu = canonical(str(body.get("gpu", "")))
            if gpu not in self.gpus:
                return 400, {"error": "not a GPU the engine uses"}
            mib = -(-body["mib"] // self.block) * self.block
            with self.lock:
                lendable = self.state()["gpus"][list(self.gpus).index(gpu)]["lendable"]
                if mib > lendable:
                    return 409, {"max_mib": lendable}
                lease = {"id": str(self.next_id), "gpu": gpu, "mib": mib}
                self.next_id += 1
                self.leases[lease["id"]] = lease
            return 200, lease
        if method == "POST" and path == "/vram/return":
            if not isinstance(body, dict) or "id" not in body:
                return 400, {"error": "id is missing"}
            lease_id = str(body["id"])
            with self.lock:
                lease = self.leases.pop(lease_id, None)
            if lease is None:
                return 404, {"error": "no open lease has this id"}
            return 200, lease
        return 404, {"error": "not found"}


class FakeSwap(Scripted):
    """llama-swap: models by name, each with a state and an engine or None."""

    def __init__(self, models: dict[str, tuple[str, FakeEngine | None]] | None = None,
                 port_base: int = 10000):
        super().__init__()
        self.models = dict(models or {})
        self.port_base = port_base

    def running(self):
        out = []
        for i, (name, (state, _engine)) in enumerate(self.models.items()):
            out.append({"model": name, "state": state,
                        "proxy": f"http://localhost:{self.port_base + i}"})
        return {"running": out}

    def handle(self, method: str, path: str, body: object):
        hit = self.scripted(method, path)
        if hit is not None:
            return hit
        if method == "GET" and path == "/running":
            return 200, self.running()
        if path.startswith("/upstream/"):
            rest = path[len("/upstream/"):]
            name, _, sub = rest.partition("/")
            sub = "/" + sub
            if name not in self.models:
                return 404, {"error": "model not found"}
            state, engine = self.models[name]
            if state != "ready" or engine is None:
                return 409, f"model {name} is not loaded; path matches upstream.ignorePaths"
            return engine.handle(method, sub, body)
        return 404, {"error": {"message": "File Not Found", "type": "not_found_error",
                               "code": 404}}


class _Handler(BaseHTTPRequestHandler):
    target: Scripted

    def log_message(self, *args):
        pass

    def _serve(self, method: str):
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode("utf-8", "replace")
        self.target.requests.append({"method": method, "path": path,
                                     "headers": dict(self.headers), "body": body})
        auth = self.headers.get("Authorization")
        if self.target.require_key and auth != f"Bearer {self.target.require_key}":
            status, answer = 401, {"error": "unauthorized"}
        else:
            status, answer = self.target.handle(method, path, body)
        if isinstance(answer, (dict, list)):
            data = json.dumps(answer).encode()
            ctype = "application/json"
        else:
            data = str(answer).encode()
            ctype = "text/plain; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


class Served:
    """A Scripted target served on a loopback port."""

    def __init__(self, target: Scripted):
        self.target = target
        handler = type("Handler", (_Handler,), {"target": target})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve():
    """serve(target) -> Served; every server is shut down after the test."""
    served: list[Served] = []

    def _serve(target: Scripted) -> Served:
        s = Served(target)
        served.append(s)
        return s

    yield _serve
    for s in served:
        s.close()

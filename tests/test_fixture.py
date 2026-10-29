"""The stand-in servers answer like the docs say an engine and llama-swap do."""

import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from conftest import FakeEngine, FakeSwap


def call(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json"} if data else {})
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def test_engine_state_lend_return(serve):
    engine = FakeEngine({"0000:0a:00.0": 2048, "01:00.0": 0})
    s = serve(engine)
    status, state = call(s.url + "/vram/state")
    assert status == 200
    assert state["gpus"] == [{"gpu": "0000:0a:00.0", "lendable": 2048, "lent": 0},
                             {"gpu": "0000:01:00.0", "lendable": 0, "lent": 0}]
    status, lease = call(s.url + "/vram/lend", "POST", {"gpu": "0A:00.0", "mib": 100})
    assert status == 200 and lease["mib"] == 128
    status, body = call(s.url + "/vram/lend", "POST", {"gpu": "0a:00.0", "mib": 4096})
    assert status == 409 and body == {"max_mib": 1920}
    status, body = call(s.url + "/vram/return", "POST", {"id": lease["id"]})
    assert status == 200
    status, body = call(s.url + "/vram/return", "POST", {"id": lease["id"]})
    assert status == 404
    assert call(s.url + "/vram/state")[1]["leases"] == []


def test_scripted_answer_overrides_once(serve):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.script("GET", "/vram/state", (503, {"error": "engine not running"}))
    s = serve(engine)
    assert call(s.url + "/vram/state")[0] == 503
    assert call(s.url + "/vram/state")[0] == 200


def test_swap_running_and_passthrough(serve):
    a = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"a": ("ready", a), "b": ("starting", None)})
    s = serve(swap)
    status, running = call(s.url + "/running")
    assert [m["model"] for m in running["running"]] == ["a", "b"]
    assert call(s.url + "/upstream/a/vram/state")[0] == 200
    status, body = call(s.url + "/upstream/b/vram/state")
    assert status == 409 and "not loaded" in body
    assert call(s.url + "/upstream/c/vram/state")[0] == 404
    assert call(s.url + "/vram/state")[0] == 404
    assert swap.requests[-1]["path"] == "/vram/state"


def test_api_key_required(serve):
    swap = FakeSwap({})
    swap.require_key = "secret"
    s = serve(swap)
    assert call(s.url + "/running")[0] == 401

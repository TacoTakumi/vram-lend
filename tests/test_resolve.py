"""Endpoint resolution: an engine, a llama-swap, or nothing with the reason."""

import json

import pytest

from conftest import FakeEngine, FakeNothing, FakeSwap
from vram_lend import lend


def resolve(*servers, key=None):
    return lend.resolve(list(servers), lend.Http(key))


def test_engine_resolves_to_itself(serve):
    s = serve(FakeEngine({"0a:00.0": 1024}))
    endpoints, notes = resolve(s.url)
    assert [e.url for e in endpoints] == [s.url]
    assert notes == []


def test_swap_resolves_to_the_ready_models_only(serve):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"01:00.0": 512})
    swap = FakeSwap({"a": ("ready", a), "loading": ("starting", None), "b": ("ready", b)})
    s = serve(swap)
    endpoints, notes = resolve(s.url)
    assert [e.url for e in endpoints] == [s.url + "/upstream/a", s.url + "/upstream/b"]
    assert notes == []
    called = [r["path"] for r in swap.requests]
    assert called == ["/vram/state", "/running"]
    for ep in endpoints:
        ep.state()
    assert not any("/upstream/loading" in r["path"] for r in swap.requests)


def test_swap_with_nothing_ready_is_a_note(serve):
    s = serve(FakeSwap({"x": ("stopped", None)}))
    endpoints, notes = resolve(s.url)
    assert endpoints == []
    assert len(notes) == 1 and "no model ready" in notes[0]


def test_unknown_server_resolves_to_nothing_with_the_reason(serve):
    s = serve(FakeNothing())
    endpoints, notes = resolve(s.url)
    assert endpoints == []
    assert len(notes) == 1
    assert "404 on /vram/state" in notes[0] and "404 on /running" in notes[0]


def test_engine_that_cannot_lend_is_a_note(serve):
    s = serve(FakeEngine(can_lend=False))
    endpoints, notes = resolve(s.url)
    assert endpoints == []
    assert len(notes) == 1 and "cannot lend (501" in notes[0]


def test_server_that_does_not_answer_is_a_note():
    endpoints, notes = resolve("http://127.0.0.1:9")
    assert endpoints == []
    assert len(notes) == 1 and "no answer" in notes[0]


def test_servers_from_flag_env_default(monkeypatch):
    assert lend.servers_from(None, {}) == [lend.DEFAULT_SERVER]
    assert lend.servers_from(None, {"VRAM_LEND_SERVER": "http://a:1, http://b:2/"}) == \
        ["http://a:1", "http://b:2"]
    assert lend.servers_from(["http://c:3/"], {"VRAM_LEND_SERVER": "http://a:1"}) == ["http://c:3"]


def test_bearer_key_on_every_call(serve):
    engine = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"m": ("ready", engine)})
    swap.require_key = "secret"
    s = serve(swap)
    endpoints, notes = resolve(s.url, key="secret")
    assert len(endpoints) == 1 and notes == []
    ep = endpoints[0]
    ep.state()
    status, lease = ep.lend("0a:00.0", 64)
    assert status == 200
    assert ep.give_back(lease["id"])[0] == 200
    paths = [r["path"] for r in swap.requests]
    assert paths == ["/vram/state", "/running", "/upstream/m/vram/state",
                     "/upstream/m/vram/lend", "/upstream/m/vram/return"]
    assert all(r["headers"].get("Authorization") == "Bearer secret" for r in swap.requests)


def test_missing_key_is_reported_as_401(serve):
    swap = FakeSwap({})
    swap.require_key = "secret"
    s = serve(swap)
    endpoints, notes = resolve(s.url)
    assert endpoints == []
    assert len(notes) == 1 and "401" in notes[0] and "VRAM_LEND_API_KEY" in notes[0]
    assert all("Authorization" not in r["headers"] for r in swap.requests)


def test_state_reads_env_key(serve, monkeypatch, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    s = serve(engine)
    engine.require_key = "k"
    monkeypatch.setenv("VRAM_LEND_API_KEY", "k")
    assert lend.main(["--server", s.url, "state", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["endpoints"][0]["gpus"][0]["lendable"] == 1024


def test_unloaded_upstream_server_is_not_loaded_and_never_probed(serve):
    """A --server of <swap>/upstream/<model> whose model is not loaded: a 409
    on /vram/state, and no call to /upstream/<model>/running, which llama-swap
    would answer by loading the model."""
    swap = FakeSwap({"m": ("stopped", None)})
    s = serve(swap)
    endpoints, notes = resolve(s.url + "/upstream/m")
    assert endpoints == []
    assert len(notes) == 1 and "not loaded" in notes[0]
    assert [r["path"] for r in swap.requests] == ["/upstream/m/vram/state"]


def test_no_running_probe_after_a_503_on_state(serve):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.script("GET", "/vram/state", (503, {"error": "engine not running"}))
    s = serve(engine)
    endpoints, notes = resolve(s.url)
    assert endpoints == []
    assert len(notes) == 1 and "503" in notes[0]
    assert [r["path"] for r in engine.requests] == ["/vram/state"]


def test_plain_swap_still_resolves_through_running(serve):
    a = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"a": ("ready", a)})
    s = serve(swap)
    endpoints, notes = resolve(s.url)
    assert [e.url for e in endpoints] == [s.url + "/upstream/a"]
    assert [r["path"] for r in swap.requests] == ["/vram/state", "/running"]

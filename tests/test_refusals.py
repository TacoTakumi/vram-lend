"""Every status the docs list maps to an exit code and a one-line cause."""

import pytest

from conftest import FakeEngine, FakeSwap
from vram_lend import lend


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(lend, "BACKOFF", [0.01, 0.01, 0.01])


def lend_cmd(s, *extra):
    return lend.main(["--server", s.url, "lend", "--gpu", "0a:00.0", "--mib", "512", *extra])


def test_409_with_max_mib_tries_the_next_endpoint_then_names_it(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (409, {"max_mib": 256}))
    b.script("POST", "/vram/lend", (409, {"max_mib": 128}))
    s = serve(FakeSwap({"a": ("ready", a), "b": ("ready", b)}))
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    err = capsys.readouterr().err
    assert "at most 256 MiB" in err and "at most 128 MiB" in err and "409" in err
    paths = [r["path"] for r in s.target.requests if r["path"].endswith("/vram/lend")]
    assert paths == ["/upstream/a/vram/lend", "/upstream/b/vram/lend"]


def test_409_without_max_mib_skips_the_endpoint(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"a": ("ready", a), "b": ("ready", b)})
    swap.script("POST", "/upstream/a/vram/lend",
                (409, "model a is not loaded; path matches upstream.ignorePaths"))
    s = serve(swap)
    assert lend_cmd(s) == 0
    assert len(b.state()["leases"]) == 1 and a.state()["leases"] == []


def test_409_not_loaded_alone_is_reported(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"a": ("ready", a)})
    swap.script("POST", "/upstream/a/vram/lend", (409, "model a is not loaded"))
    s = serve(swap)
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    assert "not loaded" in capsys.readouterr().err


def test_429_is_retried_then_succeeds(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"a": ("ready", a)})
    swap.script("POST", "/upstream/a/vram/lend", (429, "Too many requests"),
                (429, "Too many requests"))
    s = serve(swap)
    assert lend_cmd(s) == 0
    lends = [r for r in swap.requests if r["path"].endswith("/vram/lend")]
    assert len(lends) == 3


def test_429_forever_is_reported(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"a": ("ready", a)})
    swap.script("POST", "/upstream/a/vram/lend", *([(429, "Too many requests")] * 10))
    s = serve(swap)
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    err = capsys.readouterr().err
    assert "429" in err and "concurrencyLimit" in err
    assert a.state()["leases"] == []


def test_503_stops_at_once(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (503, {"error": "the engine did not answer"}))
    s = serve(FakeSwap({"a": ("ready", a), "b": ("ready", b)}))
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    err = capsys.readouterr().err
    assert "503" in err and "do not use the memory" in err
    assert b.state()["leases"] == []
    assert not any(r["path"] == "/upstream/b/vram/lend" for r in s.target.requests)


def test_501_marks_the_endpoint_as_not_lending(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (501, {"error": "the engine cannot lend"}))
    s = serve(a)
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    assert "501" in capsys.readouterr().err


def test_404_on_the_lend_route(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (404, {"error": "not found"}))
    s = serve(a)
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    err = capsys.readouterr().err
    assert "404" in err and "does not lend" in err


def test_401_names_the_key_variable(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (401, {"error": "unauthorized"}))
    s = serve(a)
    assert lend_cmd(s) == lend.EXIT_USAGE
    assert "VRAM_LEND_API_KEY" in capsys.readouterr().err


def test_400_is_a_usage_error_with_the_engine_text(serve, capsys):
    a = FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (400, {"error": "mib must be a whole number"}))
    s = serve(a)
    assert lend_cmd(s) == lend.EXIT_USAGE
    assert "mib must be a whole number" in capsys.readouterr().err


def test_502_is_reported_and_the_next_endpoint_tried(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"0a:00.0": 1024})
    a.script("POST", "/vram/lend", (502, {"error": "unknown answer"}))
    s = serve(FakeSwap({"a": ("ready", a), "b": ("ready", b)}))
    assert lend_cmd(s) == 0
    assert len(b.state()["leases"]) == 1


def test_lendable_too_small_everywhere_names_the_most(serve, capsys):
    a = FakeEngine({"0a:00.0": 256})
    s = serve(a)
    assert lend_cmd(s) == lend.EXIT_NO_MEMORY
    assert "at most 256 MiB" in capsys.readouterr().err

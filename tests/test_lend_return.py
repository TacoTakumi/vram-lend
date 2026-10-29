"""vram-lend lend and return: a lease as JSON, returned by id."""

import json

import pytest

from conftest import FakeEngine, FakeNothing, FakeSwap
from vram_lend import lend


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(lend, "BACKOFF", [0.01, 0.01, 0.01])


def run(*argv):
    return lend.main(list(argv))


def test_lend_then_return_leaves_no_open_lease(serve, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    assert run("--server", s.url, "lend", "--gpu", "0A:00.0", "--mib", "100") == 0
    lease = json.loads(capsys.readouterr().out)
    assert lease == {"id": "1", "gpu": "0000:0a:00.0", "mib": 128, "endpoint": s.url}
    assert engine.state()["leases"] != []
    assert run("--server", s.url, "return", "--id", "1") == 0
    assert "returned lease 1" in capsys.readouterr().out
    assert engine.state()["leases"] == []


def test_return_unknown_id_counts_as_returned(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 2048}))
    assert run("--server", s.url, "return", "--id", "42") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_through_swap_finds_the_endpoint(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"01:00.0": 1024})
    b.handle("POST", "/vram/lend", {"gpu": "01:00.0", "mib": 64})
    swap = FakeSwap({"a": ("ready", a), "b": ("ready", b)})
    s = serve(swap)
    assert run("--server", s.url, "return", "--id", "1") == 0
    assert f"at {s.url} model b" in capsys.readouterr().out
    assert b.state()["leases"] == []
    paths = [r["path"] for r in swap.requests]
    assert paths[-2:] == ["/upstream/a/vram/return", "/upstream/b/vram/return"]


def test_return_when_every_model_answers_404(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"01:00.0": 1024})
    s = serve(FakeSwap({"a": ("ready", a), "b": ("ready", b)}))
    assert run("--server", s.url, "return", "--id", "7") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_when_no_model_is_loaded(serve, capsys):
    s = serve(FakeSwap({"a": ("stopped", None)}))
    assert run("--server", s.url, "return", "--id", "7") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_still_in_use_then_free(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    engine.script("POST", "/vram/return", (409, {"error": "in use", "retry": True}),
                  (409, {"error": "in use", "retry": True}))
    s = serve(engine)
    assert run("--server", s.url, "return", "--id", "1") == 0
    assert engine.state()["leases"] == []


def test_return_still_in_use_forever(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    engine.script("POST", "/vram/return", *([(409, {"error": "in use", "retry": True})] * 10))
    s = serve(engine)
    assert run("--server", s.url, "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    err = capsys.readouterr().err
    assert "lease 1" in err and "still in use" in err


def test_return_with_no_answer_asks_to_retry(capsys):
    assert run("--server", "http://127.0.0.1:9", "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    assert "return it again" in capsys.readouterr().err


def test_lend_picks_the_endpoint_with_enough_lendable(serve, capsys):
    a, b = FakeEngine({"0a:00.0": 256}), FakeEngine({"0a:00.0": 2048})
    s = serve(FakeSwap({"a": ("ready", a), "b": ("ready", b)}))
    assert run("--server", s.url, "lend", "--gpu", "0a:00.0", "--mib", "1024") == 0
    lease = json.loads(capsys.readouterr().out)
    assert lease["endpoint"] == s.url + "/upstream/b"
    assert a.state()["leases"] == [] and len(b.state()["leases"]) == 1


def test_lend_card_not_listed(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 2048}))
    assert run("--server", s.url, "lend", "--gpu", "09:00.0", "--mib", "64") == lend.EXIT_NO_MEMORY
    assert "0000:09:00.0" in capsys.readouterr().err


def test_lend_bad_arguments(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 2048}))
    assert run("--server", s.url, "lend", "--gpu", "gpu0", "--mib", "64") == lend.EXIT_USAGE
    assert run("--server", s.url, "lend", "--gpu", "0a:00.0", "--mib", "0") == lend.EXIT_USAGE


def test_lend_with_nothing_resolved(serve, capsys):
    s = serve(FakeNothing())
    assert run("--server", s.url, "lend", "--gpu", "0a:00.0", "--mib", "64") == lend.EXIT_NO_MEMORY
    err = capsys.readouterr().err
    assert "no lend endpoint found" in err and "note:" in err

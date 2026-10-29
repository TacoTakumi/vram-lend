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


def test_return_text_409_not_loaded_counts_as_ended(serve, capsys):
    """llama-swap answers a text 409 for a model that is not loaded; the lease
    ended with that engine process."""
    swap = FakeSwap({"m": ("ready", FakeEngine({"0a:00.0": 1024}))})
    swap.script("POST", "/upstream/m/vram/return",
                (409, "model m is not loaded; path matches upstream.ignorePaths"))
    s = serve(swap)
    assert run("--server", s.url, "return", "--id", "1") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_json_409_without_retry_counts_as_ended(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.script("POST", "/vram/return", (409, {"max_mib": 0}))
    s = serve(engine)
    assert run("--server", s.url, "return", "--id", "1") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_endpoint_picks_the_named_engine_when_ids_collide(serve, capsys):
    """Lease ids are per engine, so two engines behind one llama-swap can both
    have a lease 1. --endpoint names the one the lend printed."""
    a, b = FakeEngine({"0a:00.0": 1024}), FakeEngine({"01:00.0": 1024})
    a.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    b.handle("POST", "/vram/lend", {"gpu": "01:00.0", "mib": 64})
    swap = FakeSwap({"a": ("ready", a), "b": ("ready", b)})
    s = serve(swap)
    assert run("return", "--id", "1", "--endpoint", s.url + "/upstream/b") == 0
    assert "returned lease 1" in capsys.readouterr().out
    assert len(a.state()["leases"]) == 1 and b.state()["leases"] == []
    assert [r["path"] for r in swap.requests] == ["/upstream/b/vram/state", "/upstream/b/vram/return"]


def test_return_endpoint_unreachable_exits_3(capsys):
    assert run("return", "--id", "1", "--endpoint", "http://127.0.0.1:9") == lend.EXIT_LEASE_OPEN
    assert "return it again" in capsys.readouterr().err


def test_return_endpoint_404_is_already_gone(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert run("return", "--id", "9", "--endpoint", s.url) == 0
    assert "already gone" in capsys.readouterr().out


def test_return_with_an_unreachable_server_beside_a_404_exits_3(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert run("--server", "http://127.0.0.1:9", "--server", s.url,
               "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    err = capsys.readouterr().err
    assert "127.0.0.1:9" in err and "return it again" in err


def test_return_with_an_unreachable_server_beside_the_holder_returns(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    s = serve(engine)
    assert run("--server", "http://127.0.0.1:9", "--server", s.url, "return", "--id", "1") == 0
    assert engine.state()["leases"] == []


def test_return_uncertain_401_server_exits_3(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    engine.require_key = "k"
    s = serve(engine)
    assert run("--server", s.url, "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    err = capsys.readouterr().err
    assert "VRAM_LEND_API_KEY" in err and "return it again" in err
    assert len(engine.state()["leases"]) == 1


@pytest.mark.parametrize("status,body", [(429, "Too many requests"),
                                         (503, {"error": "engine not running"})])
def test_return_uncertain_status_on_state_exits_3(serve, capsys, status, body):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    swap = FakeSwap({"m": ("ready", engine)})
    swap.script("GET", "/upstream/m/vram/state", (status, body))
    s = serve(swap)
    assert run("--server", s.url + "/upstream/m", "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    assert "return it again" in capsys.readouterr().err


def test_return_definite_no_api_server_beside_a_404_is_gone(serve, capsys):
    s_none = serve(FakeNothing())
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert run("--server", s_none.url, "--server", s.url, "return", "--id", "1") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_endpoint_swap_root_is_not_a_lend_endpoint(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    s = serve(FakeSwap({"m": ("ready", engine)}))
    assert run("return", "--id", "1", "--endpoint", s.url) == lend.EXIT_LEASE_OPEN
    err = capsys.readouterr().err
    assert "not a lend endpoint" in err
    assert len(engine.state()["leases"]) == 1


def test_return_endpoint_unloaded_model_is_gone(serve, capsys):
    s = serve(FakeSwap({"m": ("stopped", None)}))
    assert run("return", "--id", "1", "--endpoint", s.url + "/upstream/m") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_endpoint_with_the_lease_returns_it(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    s = serve(engine)
    assert run("return", "--id", "1", "--endpoint", s.url + "/") == 0
    assert engine.state()["leases"] == []


@pytest.mark.parametrize("status,body", [(503, "busy"), (429, "Too many requests")])
def test_return_running_uncertain_beside_a_404_exits_3(serve, capsys, status, body):
    odd = FakeNothing()
    odd.script("GET", "/running", (status, body))
    s_odd = serve(odd)
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert run("--server", s_odd.url, "--server", s.url, "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    assert "return it again" in capsys.readouterr().err


def test_return_definite_no_api_with_a_foreign_200_on_running(serve, capsys):
    odd = FakeNothing()
    odd.script("GET", "/running", (200, {"status": "ok"}))
    s_odd = serve(odd)
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert run("--server", s_odd.url, "--server", s.url, "return", "--id", "1") == 0
    assert "already gone" in capsys.readouterr().out


def test_return_endpoint_engine_that_cannot_lend_is_gone(serve, capsys):
    s = serve(FakeEngine(can_lend=False))
    assert run("return", "--id", "1", "--endpoint", s.url) == 0
    assert "already gone" in capsys.readouterr().out


@pytest.mark.parametrize("status", [403, 405])
def test_return_running_other_4xx_beside_a_404_exits_3(serve, capsys, status):
    odd = FakeNothing()
    odd.script("GET", "/running", (status, {"error": "no"}))
    s_odd = serve(odd)
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert run("--server", s_odd.url, "--server", s.url, "return", "--id", "1") == lend.EXIT_LEASE_OPEN
    assert "return it again" in capsys.readouterr().err

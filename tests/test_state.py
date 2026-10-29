"""vram-lend state: every endpoint with its cards, and the PCI address forms."""

import json

import pytest

from conftest import FakeEngine, FakeNothing, FakeSwap
from vram_lend import lend


@pytest.mark.parametrize("form", ["0a:00.0", "0000:0A:00.0", "00000000:0a:00.0", "0000:0a:00.0"])
def test_pci_forms_normalise_to_one(form):
    assert lend.normalize_pci(form) == "0000:0a:00.0"


@pytest.mark.parametrize("bad", ["gpu0", "0", "0a:00", "0000:0a:00.8", ""])
def test_not_a_pci_address(bad):
    assert lend.normalize_pci(bad) is None


def test_state_lists_every_endpoint_and_card(serve, capsys):
    a = FakeEngine({"0a:00.0": 2048, "01:00.0": 0})
    b = FakeEngine({"0a:00.0": 512})
    s = serve(FakeSwap({"a": ("ready", a), "b": ("ready", b)}))
    assert lend.main(["--server", s.url, "state"]) == 0
    out = capsys.readouterr().out
    assert f"{s.url} model a" in out and f"{s.url} model b" in out
    assert "0000:0a:00.0  lendable 2048 MiB  lent 0 MiB" in out
    assert "0000:01:00.0  lendable 0 MiB  lent 0 MiB" in out
    assert "0000:0a:00.0  lendable 512 MiB" in out


def test_state_gpu_filter_accepts_every_form(serve, capsys):
    s = serve(FakeEngine({"0000:0a:00.0": 1024, "01:00.0": 1024}))
    for form in ("0a:00.0", "0000:0A:00.0", "00000000:0a:00.0"):
        assert lend.main(["--server", s.url, "state", "--gpu", form, "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [g["gpu"] for g in out["endpoints"][0]["gpus"]] == ["0000:0a:00.0"]


def test_state_gpu_filter_card_not_listed(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert lend.main(["--server", s.url, "state", "--gpu", "09:00.0"]) == 0
    assert "no GPU listed for 0000:09:00.0" in capsys.readouterr().out


def test_state_bad_gpu_argument(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 1024}))
    assert lend.main(["--server", s.url, "state", "--gpu", "gpu0"]) == 2
    assert "not a PCI address" in capsys.readouterr().err


def test_state_shows_leases_and_problems(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    engine.handle("POST", "/vram/lend", {"gpu": "0a:00.0", "mib": 64})
    off = FakeEngine(can_lend=False)
    s = serve(FakeSwap({"on": ("ready", engine), "off": ("ready", off)}))
    assert lend.main(["--server", s.url, "state"]) == 0
    out = capsys.readouterr().out
    assert "lease 1  0000:0a:00.0  64 MiB" in out
    assert "the engine cannot lend (501)" in out


def test_state_with_nothing_resolved_exits_1(serve, capsys):
    s = serve(FakeNothing())
    assert lend.main(["--server", s.url, "state"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("note: ") and "no VRAM Lend API" in out


def test_state_model_unloaded_between_running_and_state(serve, capsys):
    engine = FakeEngine({"0a:00.0": 1024})
    swap = FakeSwap({"m": ("ready", engine)})
    swap.script("GET", "/upstream/m/vram/state",
                (409, "model m is not loaded; path matches upstream.ignorePaths"))
    s = serve(swap)
    assert lend.main(["--server", s.url, "state"]) == 0
    assert "not loaded (409)" in capsys.readouterr().out

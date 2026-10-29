"""vram-lend run: lend, run the command, always return the lease."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import FakeEngine, FakeSwap
from vram_lend import lend


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(lend, "BACKOFF", [0.01, 0.01, 0.01])


def run(s, *command, mib="100"):
    return lend.main(["--server", s.url, "run", "--gpu", "0a:00.0", "--mib", mib, "--", *command])


def test_exit_0_returns_the_lease_and_passes_the_environment(serve, capsys, tmp_path):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    out = tmp_path / "env.json"
    script = ("import json, os, sys; json.dump({k: v for k, v in os.environ.items() "
              "if k.startswith('VRAM_LEND_')}, open(sys.argv[1], 'w'))")
    assert run(s, sys.executable, "-c", script, str(out)) == 0
    seen = json.loads(out.read_text())
    assert seen == {"VRAM_LEND_LEASE": "1", "VRAM_LEND_MIB": "128",
                    "VRAM_LEND_GPU": "0000:0a:00.0", "VRAM_LEND_ENDPOINT": s.url}
    assert engine.state()["leases"] == []
    paths = [r["path"] for r in engine.requests]
    assert paths[-2:] == ["/vram/lend", "/vram/return"]
    err = capsys.readouterr().err
    assert "lent 128 MiB" in err and "returned lease 1" in err


def test_exit_1_returns_the_lease_and_keeps_the_code(serve):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    assert run(s, "sh", "-c", "exit 1") == 1
    assert engine.state()["leases"] == []


def test_exit_7_keeps_the_code(serve):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    assert run(s, "sh", "-c", "exit 7") == 7
    assert engine.state()["leases"] == []


def test_command_killed_by_sigterm_returns_the_lease(serve):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    assert run(s, "sh", "-c", "kill -TERM $$") == 143
    assert engine.state()["leases"] == []


def test_command_not_found_returns_the_lease(serve, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    assert run(s, "/no/such/command") == 127
    assert engine.state()["leases"] == []
    assert "cannot start" in capsys.readouterr().err


def _wrapper(s, *command):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [sys.executable, "-m", "vram_lend.lend", "--server", s.url, "run",
         "--gpu", "0a:00.0", "--mib", "100", "--", *command],
        env=env, stderr=subprocess.PIPE, text=True)


def _wait_for_lease(engine, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if engine.state()["leases"]:
            return
        time.sleep(0.05)
    raise AssertionError("the lease never appeared")


@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_signal_to_the_wrapper_is_forwarded_and_the_lease_returned(serve, sig, code):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    proc = _wrapper(s, "sleep", "30")
    _wait_for_lease(engine)
    time.sleep(0.2)
    proc.send_signal(sig)
    _out, err = proc.communicate(timeout=10)
    assert proc.returncode == code, err
    assert "returned lease" in err
    assert engine.state()["leases"] == []


def test_return_in_use_twice_then_free_ends_clean(serve, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    engine.script("POST", "/vram/return", (409, {"error": "in use", "retry": True}),
                  (409, {"error": "in use", "retry": True}))
    s = serve(engine)
    assert run(s, "true") == 0
    assert engine.state()["leases"] == []


def test_return_in_use_forever_is_reported_with_the_id(serve, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    engine.script("POST", "/vram/return", *([(409, {"error": "in use", "retry": True})] * 10))
    s = serve(engine)
    assert run(s, "true") == lend.EXIT_LEASE_OPEN
    err = capsys.readouterr().err
    assert "lease 1" in err and "still in use" in err
    assert len(engine.state()["leases"]) == 1


def test_return_failure_does_not_hide_the_command_failure(serve, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    engine.script("POST", "/vram/return", *([(409, {"error": "in use", "retry": True})] * 10))
    s = serve(engine)
    assert run(s, "sh", "-c", "exit 5") == 5
    assert "lease 1" in capsys.readouterr().err


def test_503_never_starts_the_command(serve, capsys, tmp_path):
    engine = FakeEngine({"0a:00.0": 2048})
    engine.script("POST", "/vram/lend", (503, {"error": "the engine did not answer"}))
    s = serve(engine)
    marker = tmp_path / "started"
    assert run(s, "touch", str(marker)) == lend.EXIT_NO_MEMORY
    assert not marker.exists()
    assert "503" in capsys.readouterr().err


def test_no_command_is_a_usage_error(serve, capsys):
    s = serve(FakeEngine({"0a:00.0": 2048}))
    assert lend.main(["--server", s.url, "run", "--gpu", "0a:00.0", "--mib", "64"]) == lend.EXIT_USAGE
    assert "needs a command" in capsys.readouterr().err


def test_run_through_swap_returns_on_the_same_model(serve):
    a, b = FakeEngine({"0a:00.0": 256}), FakeEngine({"0a:00.0": 2048})
    swap = FakeSwap({"a": ("ready", a), "b": ("ready", b)})
    s = serve(swap)
    assert run(s, "true", mib="1024") == 0
    assert a.state()["leases"] == [] and b.state()["leases"] == []
    returns = [r["path"] for r in swap.requests if r["path"].endswith("/vram/return")]
    assert returns == ["/upstream/b/vram/return"]


def test_interrupt_before_start_still_returns_the_lease(serve, monkeypatch, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(lend.subprocess, "Popen", boom)
    assert run(s, "true") == 130
    assert engine.state()["leases"] == []
    assert "returned lease 1" in capsys.readouterr().err


def test_handlers_during_return_forward_and_are_restored_after(serve, monkeypatch):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    seen = {}
    real = lend.return_lease

    def spy(endpoints, lease_id):
        seen.update({sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)})
        return real(endpoints, lease_id)

    monkeypatch.setattr(lend, "return_lease", spy)
    assert run(s, "true") == 0
    for sig, handler in seen.items():
        assert callable(handler) and handler is not before[sig], sig
    for sig, handler in before.items():
        assert signal.getsignal(sig) is handler, sig


def test_second_signal_kills_a_command_that_ignores_the_first(serve):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    ignore_term = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                   "time.sleep(30)")
    proc = _wrapper(s, sys.executable, "-c", ignore_term)
    _wait_for_lease(engine)
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    assert proc.poll() is None, "the first SIGTERM must not kill a command that ignores it"
    proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=10)
    assert proc.returncode == 137, err
    assert "returned lease" in err
    assert engine.state()["leases"] == []


def _lend_with_signal(sig, times):
    real = lend.lend_on

    def lend_on_and_signal(endpoints, gpu, mib):
        lease = real(endpoints, gpu, mib)
        for _ in range(times):
            os.kill(os.getpid(), sig)
        return lease

    return lend_on_and_signal


def test_signal_during_lend_prints_and_returns_without_starting(serve, monkeypatch, capsys, tmp_path):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    monkeypatch.setattr(lend, "lend_on", _lend_with_signal(signal.SIGINT, 1))
    marker = tmp_path / "started"
    assert run(s, "touch", str(marker)) == 130
    err = capsys.readouterr().err
    assert "lend is still in flight" in err and "will be returned" in err
    assert "interrupted before the command started" in err
    assert not marker.exists()
    assert engine.state()["leases"] == []


def test_second_signal_during_lend_says_not_to_kill(serve, monkeypatch, capsys):
    engine = FakeEngine({"0a:00.0": 2048})
    s = serve(engine)
    monkeypatch.setattr(lend, "lend_on", _lend_with_signal(signal.SIGTERM, 2))
    assert run(s, "true") == 143
    err = capsys.readouterr().err
    assert "do not kill" in err
    assert engine.state()["leases"] == []

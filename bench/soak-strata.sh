#!/bin/bash
# Strata soak of VRAM Lend: lend/return cycles on alternating GPUs, each while a streaming request runs, then
# (optional) a command run while HOLD_MIB is lent on every GPU, for example a quality suite.
#
# Each cycle starts one streaming request (temperature 0, "count from 1 to COUNT_TO, one number per line"), waits
# for its first tokens, lends a size between MIN_MIB and MAX_MIB on the cycle's GPU, waits HOLD_S, returns the lease
# and lets the request finish. Recorded per cycle: every lend and return answer with its time, the engine process's
# memory on the GPU before, while lent and after (nvidia-smi), the largest token gap across the lend and the return,
# the request's finish, error and whether its numbers run 1, 2, 3 ... without a gap, and the open leases after.
# A cycle passes when the lend and the return answer 200, the request is still streaming when the return is sent,
# it finishes without an error and in order, the engine's memory on the GPU drops by at least the lease and comes
# back, and no lease is left open.
#
# Environment:
#   MODEL       model id the server serves (required)
#   BASE_URL    OpenAI-compatible server (default http://127.0.0.1:8080, llama-swap)
#   VRAM_URL    where /vram/* is served (default $BASE_URL/upstream/$MODEL, llama-swap's passthrough;
#               for a Strata server reached directly, set it to $BASE_URL)
#   GPUS        PCI addresses to alternate, space separated (default "0000:01:00.0 0000:02:00.0")
#   CYCLES      lend/return cycles (default 50; 0 skips the soak)
#   MIN_MIB, MAX_MIB  lease sizes, varied in 512 MiB steps (default 2048 and 8192)
#   HOLD_S      seconds a lease stays out during its request (default 1.0)
#   COUNT_TO    the request counts to this number (default 300; long enough to outlast a lend and a return)
#   ENGINE_LOG  the engine's log file: the lines it gains during the run are counted for CUDA errors, non-finite
#               logits warnings and engine starts, and saved next to OUT (optional)
#   HOLD_MIB    lease size for the hold step (default 8192)
#   HOLD_CMD    a shell command run while HOLD_MIB is lent on every GPU of GPUS (optional: no hold step without it)
#   ENGINE_MATCH  regex on `pgrep -af` lines of the engine's processes (default "/build/strata( |$)")
#   OUT         markdown results file, appended (default results/soak-strata-run.md); the per-cycle records go to
#               the same name with .jsonl
#   PYTHON      Python with requests (default python3)
#
# The model is loaded by the first request if the server has not loaded it. Nothing is unloaded at the end.
set -u
: "${MODEL:?set MODEL to the model id}"
HERE=$(cd "$(dirname "$0")" && pwd)
export MODEL HERE
export BASE_URL=${BASE_URL:-http://127.0.0.1:8080}
export VRAM_URL=${VRAM_URL:-$BASE_URL/upstream/$MODEL}
export GPUS=${GPUS:-0000:01:00.0 0000:02:00.0}
export CYCLES=${CYCLES:-50} MIN_MIB=${MIN_MIB:-2048} MAX_MIB=${MAX_MIB:-8192} HOLD_S=${HOLD_S:-1.0}
export COUNT_TO=${COUNT_TO:-300} ENGINE_LOG=${ENGINE_LOG:-} HOLD_MIB=${HOLD_MIB:-8192} HOLD_CMD=${HOLD_CMD:-}
export ENGINE_MATCH=${ENGINE_MATCH:-/build/strata( |\$)}
export OUT=${OUT:-$HERE/results/soak-strata-run.md}
"${PYTHON:-python3}" - <<'PY'
import json, os, re, subprocess, sys, threading, time
import requests
sys.path.insert(0, os.environ["HERE"])
sys.dont_write_bytecode = True   # no __pycache__ next to live.py
import live

E = os.environ
MODEL, BASE, VURL = E["MODEL"], E["BASE_URL"].rstrip("/"), E["VRAM_URL"].rstrip("/")
GPUS = [live.norm(g) for g in E["GPUS"].split()]
CYCLES, MIN_MIB, MAX_MIB = int(E["CYCLES"]), int(E["MIN_MIB"]), int(E["MAX_MIB"])
HOLD_S, COUNT_TO, HOLD_MIB, HOLD_CMD = float(E["HOLD_S"]), int(E["COUNT_TO"]), int(E["HOLD_MIB"]), E["HOLD_CMD"]
ELOG, OUT = E["ENGINE_LOG"], E["OUT"]
JOUT = re.sub(r"\.md$", "", OUT) + ".jsonl"
live.SWAP, live.ENGINE_MATCH = BASE, E["ENGINE_MATCH"]
PROMPT = f"Count from 1 to {COUNT_TO}, one number per line. Write only the numbers."
log = []


def note(s):
    print(s, flush=True)
    log.append(s)


def state():
    r = requests.get(VURL + "/vram/state", timeout=60)
    return r.status_code, r.json()


def post(path, body):
    t = time.time()
    r = requests.post(VURL + path, json=body, timeout=120)
    try:
        j = r.json()
    except ValueError:
        j = {"text": r.text[:300]}
    return r.status_code, j, t, time.time()


def give_back(lid, limit=30.0):
    """POST /vram/return; a 409 (the memory is not free yet) is retried until `limit` seconds: (code, body, t0, t1, retries)."""
    t0, n = time.time(), 0
    while True:
        code, body, _, t1 = post("/vram/return", {"id": lid})
        if code != 409 or t1 - t0 > limit:
            return code, body, t0, t1, n
        n += 1
        time.sleep(0.05)


class Count(threading.Thread):
    """One streaming request at temperature 0 that counts; times every delta."""
    def __init__(self):
        super().__init__(daemon=True)
        self.times, self.text, self.finish, self.error, self.usage, self.t0, self.t_end = [], "", None, None, None, None, None

    def run(self):
        self.t0 = time.time()
        try:
            r = requests.post(f"{BASE}/v1/chat/completions", stream=True, timeout=1800, json={
                "model": MODEL, "stream": True, "max_tokens": COUNT_TO * 5 + 64, "temperature": 0,
                "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": PROMPT}]})
            if r.status_code != 200:
                self.error = f"HTTP {r.status_code}: {r.text[:300]}"
                return
            for raw in r.iter_lines():
                if not raw or not raw.startswith(b"data: "):
                    continue
                if raw[6:] == b"[DONE]":
                    break
                j = json.loads(raw[6:])
                if j.get("error"):
                    self.error = json.dumps(j["error"])[:300]
                if j.get("usage"):
                    self.usage = j["usage"]
                for c in j.get("choices", []):
                    d = c.get("delta") or {}
                    piece = (d.get("content") or "") + (d.get("reasoning_content") or "")
                    if piece:
                        self.times.append(time.time())
                        self.text += piece
                    if c.get("finish_reason"):
                        self.finish = c["finish_reason"]
        except Exception as e:   # noqa: BLE001 - recorded as the request's failure
            self.error = repr(e)
        finally:
            self.t_end = time.time()

    def gap(self, a, b):
        ts = [t for t in self.times if a - 1.0 <= t <= b + 3.0]
        g = [y - x for x, y in zip(ts, ts[1:]) if y >= a - 0.2]
        return round(max(g), 3) if g else None

    def count_ok(self):
        """(numbers run 1..n without a gap, n). A request cut at max_tokens may end in a partial number."""
        n = [int(x) for x in re.findall(r"\d+", self.text)]
        if self.finish == "length" and n:
            n = n[:-1]
        return bool(n) and n == list(range(1, len(n) + 1)), len(n)


def leases_open():
    _, st = state()
    return st.get("leases") or [], {c["gpu"]: c.get("lent") for c in st.get("gpus", [])}


def log_size():
    return os.path.getsize(ELOG) if ELOG and os.path.exists(ELOG) else 0


def log_span(start):
    if not ELOG or not os.path.exists(ELOG):
        return ""
    with open(ELOG, "rb") as f:
        f.seek(start)
        return f.read().decode("utf-8", "replace")


def sizes(i):
    steps = (MAX_MIB - MIN_MIB) // 512 + 1
    return MIN_MIB + ((i * 7) % steps) * 512


rows, recs = [], []
note(f"# soak {MODEL} {time.strftime('%Y-%m-%d %H:%M:%S')}: {CYCLES} cycles of {MIN_MIB}-{MAX_MIB} MiB on {GPUS}")
log0 = log_size()
t_load = time.time()
r = requests.post(f"{BASE}/v1/chat/completions", timeout=900, json={
    "model": MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": "Say OK."}],
    "chat_template_kwargs": {"enable_thinking": False}})
note(f"load/answer {r.status_code} in {time.time() - t_load:.0f} s")
code, st0 = state()
note(f"state {code}: {json.dumps(st0)}")
smi = live.Smi()
smi.pids = live.engine_pids()
note(f"engine pids {sorted(smi.pids)}")
time.sleep(1.0)

for i in range(CYCLES):
    card, mib = GPUS[i % len(GPUS)], sizes(i)
    rec = {"cycle": i + 1, "gpu": card, "mib": mib}
    s = Count()
    s.start()
    while len(s.times) < 20 and s.is_alive():
        time.sleep(0.02)
    pre = smi.now()
    code, body, t_send, t_ans = post("/vram/lend", {"gpu": card, "mib": mib})
    rec["lend"] = {"code": code, "body": body, "s": round(t_ans - t_send, 3)}
    if code == 200:
        t_free = smi.first_time(t_send, lambda b: pre.get(card, 0) - b.get(card, 0) >= mib)
        time.sleep(HOLD_S)
        mid = smi.now()
        alive_ret = s.is_alive()
        rc, rbody, r_send, r_ans, retries = give_back(body["id"])
        t_back = smi.first_time(r_send, lambda b: b.get(card, 0) >= pre.get(card, 0) - 64)
        rec["return"] = {"code": rc, "body": rbody, "s": round(r_ans - r_send, 3), "retries_409": retries}
    else:
        t_free = t_back = None
        mid, alive_ret, rc, r_send, r_ans = {}, s.is_alive(), None, t_ans, t_ans
        rec["return"] = None
    s.join()
    post_mem = smi.now()
    ok_count, n = s.count_ok()
    leases, lent = leases_open()
    rec.update(engine_mib={"before": pre.get(card), "lent": mid.get(card), "after": post_mem.get(card)},
               freed_s=round(t_free - t_send, 3) if t_free else None, back_s=round(t_back - r_send, 3) if t_back else None,
               gap_lend=s.gap(t_send, t_ans), gap_return=s.gap(r_send, r_ans), stream_alive_at_return=alive_ret,
               stream={"finish": s.finish, "error": s.error, "deltas": len(s.times), "count_ok": ok_count, "numbers": n,
                       "s": round((s.t_end or time.time()) - s.t0, 2), "completion_tokens": (s.usage or {}).get("completion_tokens")},
               leases_after=leases, lent_after=lent)
    drop = (pre.get(card) or 0) - (mid.get(card) or 0)
    ok = (code == 200 and rc == 200 and alive_ret and s.error is None and s.finish in ("stop", "length") and ok_count
          and not leases and not any(lent.values()) and drop >= mib and t_back is not None)
    rec["ok"] = bool(ok)
    recs.append(rec)
    with open(JOUT, "a") as f:
        f.write(json.dumps(rec) + "\n")
    note(f"cycle {i + 1:2d} {card} {mib:5d} MiB: lend {code} {rec['lend']['s']:.3f} s, return {rc} "
         f"{rec['return']['s'] if rec['return'] else '-'} s, engine MiB {pre.get(card)} -> {mid.get(card)} -> "
         f"{post_mem.get(card)}, gaps {rec['gap_lend']} / {rec['gap_return']} s, stream {s.finish} err {s.error} "
         f"numbers 1..{n} {'ok' if ok_count else 'BROKEN'}, alive at return {alive_ret} -> {'ok' if ok else 'FAIL'}")
    rows.append(rec)

hold = None
if HOLD_CMD:
    note(f"# hold: {HOLD_MIB} MiB on each of {GPUS}, then: {HOLD_CMD}")
    pre = smi.now()
    got = []
    for card in GPUS:
        code, body, t0, t1 = post("/vram/lend", {"gpu": card, "mib": HOLD_MIB})
        note(f"hold lend {card} {HOLD_MIB}: {code} {body} in {t1 - t0:.3f} s")
        if code == 200:
            got.append(body)
    time.sleep(1.0)
    mid = smi.now()
    _, st_mid = state()
    note(f"engine MiB {pre} -> {mid}; state {json.dumps(st_mid)}")
    t0 = time.time()
    p = subprocess.run(HOLD_CMD, shell=True)
    t_cmd = time.time() - t0
    _, st_end = state()
    note(f"command rc {p.returncode} in {t_cmd:.0f} s; state at its end {json.dumps(st_end)}")
    rets = []
    for b in got:
        rc, rb, r0, r1, n = give_back(b["id"])
        rets.append((b["gpu"], rc, round(r1 - r0, 3), n))
        note(f"hold return {b['id']} on {b['gpu']}: {rc} {rb} in {r1 - r0:.3f} s (409 retries {n})")
    time.sleep(1.0)
    post_mem = smi.now()
    leases, lent = leases_open()
    note(f"engine MiB after the returns {post_mem}; leases {leases} lent {lent}")
    hold = dict(lends=[(b["gpu"], b["mib"]) for b in got], rc=p.returncode, s=round(t_cmd), returns=rets,
                pre=pre, mid=mid, post=post_mem, leases_after=leases, lent_during=st_end.get("gpus"))
smi.stop()

span = log_span(log0)
counts = {k: len(re.findall(k, span)) for k in ("CUDA error", "non-finite", "engine started")}
if ELOG:
    with open(re.sub(r"\.md$", "", OUT) + "-engine.log", "w") as f:
        f.write(span)
note(f"engine log span: {len(span.splitlines())} lines; counts {counts}")
with open(OUT, "a") as f:
    f.write(f"\n## soak {MODEL} {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    if rows:
        okn = sum(r["ok"] for r in rows)
        def mx(k, sub=None):
            v = [(r[k][sub] if sub else r[k]) for r in rows if r.get(k) is not None and (not sub or r[k].get(sub) is not None)]
            return max(v) if v else None
        f.write(f"{okn}/{len(rows)} cycles passed. Largest lend answer {mx('lend', 's')} s, return answer "
                f"{mx('return', 's')} s, token gap across a lend {mx('gap_lend')} s, across a return {mx('gap_return')} s; "
                f"409 retries {sum((r['return'] or {}).get('retries_409', 0) for r in rows)}.\n\n")
        f.write("| cycle | GPU | MiB | lend | return | engine MiB before / lent / after | gaps s | request | ok |\n"
                "|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            ret = r["return"] or {}
            m = r["engine_mib"]
            f.write(f"| {r['cycle']} | {r['gpu']} | {r['mib']} | {r['lend']['code']} {r['lend']['s']} s | "
                    f"{ret.get('code')} {ret.get('s')} s | {m['before']} / {m['lent']} / {m['after']} | "
                    f"{r['gap_lend']} / {r['gap_return']} | {r['stream']['finish']}, 1..{r['stream']['numbers']} "
                    f"{'in order' if r['stream']['count_ok'] else 'BROKEN'}{', ' + r['stream']['error'] if r['stream']['error'] else ''} | "
                    f"{'PASS' if r['ok'] else 'FAIL'} |\n")
    if hold:
        f.write(f"\nHold: lent {hold['lends']}, command rc {hold['rc']} in {hold['s']} s, returns {hold['returns']}, "
                f"engine MiB {hold['pre']} -> {hold['mid']} -> {hold['post']}, leases after {hold['leases_after']}.\n")
    if ELOG:
        f.write(f"\nEngine log lines added during the run: {counts}.\n")
    f.write("\n<details><summary>log</summary>\n\n```\n" + "\n".join(log) + "\n```\n</details>\n")
PY

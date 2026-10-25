#!/usr/bin/env python3
"""Live checks of VRAM Lend on a served model, written as a markdown table to <out.md>.

Usage: live.py <model id> <out.md> [--checks a,b,...] [--gpu 0000:09:00.0] [--prompt-gpu 0000:05:00.0]
       live.py --hog <PCI address>    (hold most of a GPU's free memory until killed: a borrower for the busy check)

By default the model is served by llama-swap at --base-url (env VRAM_LEND_BASE_URL, default http://127.0.0.1:8080):
chat requests go to <base>/v1/chat/completions, the /vram routes are reached through llama-swap's
<base>/upstream/<model>/ passthrough (--vram-url), and the restart check unloads the model with
<base>/api/models/unload/<model> (--unload-url), or stops only the engine with --restart-url. The engine's processes
are found with pgrep (--engine-match). GPU memory of the engine process is read from `nvidia-smi --query-compute-apps ... -lms 50` (a 50 ms poll on its own thread). Streamed tokens are timed as
they arrive, so the largest gap across a lend or a return is the decode stall.
"""
import argparse, ctypes, json, os, re, subprocess, sys, threading, time
import requests

SWAP = os.environ.get("VRAM_LEND_BASE_URL", "http://127.0.0.1:8080")
VRAM_URL = "{base}/upstream/{model}"      # where /vram/* is served
UNLOAD_URL = "{base}/api/models/unload/{model}"
ENGINE_MATCH = r"/build/maya(-ds4)?( |$)"  # pgrep -af lines of the engine's processes
MIB = 1 << 20


class Smi:
    """nvidia-smi compute-apps at 50 ms: {bus: MiB} of the engine's processes, with the time read."""
    def __init__(self):
        self.samples = []   # (t, {bus: mib})
        self.pids = set()
        self.p = subprocess.Popen(["nvidia-smi", "--query-compute-apps=pid,gpu_bus_id,used_memory",
                                   "--format=csv,noheader,nounits", "-lms", "50"],
                                  stdout=subprocess.PIPE, text=True, bufsize=1)
        self.cur, self.last_t = {}, 0.0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        batch, bt, lt = {}, None, 0.0
        for line in self.p.stdout:
            t = time.time()
            if bt is not None and t - lt > 0.02:   # a poll's lines come together: a pause starts the next poll
                self.samples.append((bt, batch))
                batch, bt = {}, None
            if bt is None:
                bt = t
            lt = t
            parts = [x.strip() for x in line.split(",")]
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            pid, bus, mib = int(parts[0]), norm(parts[1]), int(parts[2])
            if pid in self.pids:
                batch[bus] = batch.get(bus, 0) + mib

    def at(self, t):
        best = None
        for st, b in self.samples:
            if st <= t:
                best = b
            else:
                break
        return dict(best or {})

    def now(self):
        time.sleep(0.15)
        return dict(self.samples[-1][1]) if self.samples else {}

    def first_time(self, t0, cond, timeout=5.0):
        """First sample time >= t0 where cond(sample) holds, or None."""
        end = t0 + timeout
        while time.time() < end + 0.2:
            for st, b in list(self.samples):
                if st >= t0 and cond(b):
                    return st
            time.sleep(0.05)
        return None

    def stop(self):
        self.p.kill()


def norm(bus):
    """00000000:09:00.0, 0000:09:00.0 or 09:00.0 -> 0000:09:00.0: lower case, the real domain in 4 hex digits (0 when
    none is given), as Maya's gpu_map.pci_addr() spells it."""
    m = re.match(r"(?:([0-9a-fA-F]{1,8}):)?([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])", bus.strip())
    if not m or int(m.group(1) or "0", 16) > 0xFFFF:
        return bus.lower()
    return f"{int(m.group(1) or '0', 16):04x}:{m.group(2)}:{m.group(3)}.{m.group(4)}".lower()


def gpu_used():
    """{bus: MiB} used on every GPU, all processes (nvidia-smi --query-gpu)."""
    out = subprocess.run(["nvidia-smi", "--query-gpu=pci.bus_id,memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return {norm(b): int(m) for b, m in (l.split(",") for l in out.strip().splitlines() if l.count(",") == 1)}


def engine_pids():
    out = subprocess.run(["pgrep", "-af", "."], capture_output=True, text=True).stdout
    return {int(l.split()[0]) for l in out.splitlines() if re.search(ENGINE_MATCH, l.partition(" ")[2])}


class Swap:
    def __init__(self, model):
        self.model = model
        self.up = VRAM_URL.format(base=SWAP, model=model)

    def state(self):
        r = requests.get(self.up + "/vram/state", timeout=60)
        return r.status_code, r.json()

    def lend(self, card, mib):
        t = time.time()
        r = requests.post(self.up + "/vram/lend", json={"gpu": card, "mib": mib}, timeout=120)
        return r.status_code, r.json(), t, time.time()

    def give_back(self, lid):
        t = time.time()
        r = requests.post(self.up + "/vram/return", json={"id": lid}, timeout=120)
        return r.status_code, r.json(), t, time.time()

    def load(self):
        t = time.time()
        r = requests.post(f"{SWAP}/v1/chat/completions", json={
            "model": self.model, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 8,
            "chat_template_kwargs": {"enable_thinking": False}}, timeout=900)
        return r.status_code, time.time() - t, r.json()

    def unload(self):
        requests.post(UNLOAD_URL.format(base=SWAP, model=self.model), timeout=300)


class Stream(threading.Thread):
    """One streaming chat request; times every delta."""
    def __init__(self, model, prompt, max_tokens=1200, think=False):
        super().__init__(daemon=True)
        self.model, self.prompt, self.max_tokens, self.think = model, prompt, max_tokens, think
        self.times, self.finish, self.error, self.t0, self.usage, self.text = [], None, None, None, None, ""

    def run(self):
        self.t0 = time.time()
        try:
            r = requests.post(f"{SWAP}/v1/chat/completions", stream=True, timeout=1800, json={
                "model": self.model, "stream": True, "max_tokens": self.max_tokens,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": self.think},
                "messages": [{"role": "user", "content": self.prompt}]})
            if r.status_code != 200:
                self.error = f"HTTP {r.status_code}: {r.text[:300]}"
                return
            for raw in r.iter_lines():
                if not raw or not raw.startswith(b"data: "):
                    continue
                data = raw[6:]
                if data == b"[DONE]":
                    break
                j = json.loads(data)
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

    def max_gap(self, a, b):
        ts = [t for t in self.times if a - 1.0 <= t <= b + 3.0]
        gaps = [y - x for x, y in zip(ts, ts[1:]) if y >= a - 0.2]
        return max(gaps) if gaps else None

    def first_token(self):
        return self.times[0] - self.t0 if self.times else None

    def rate(self, skip=5):
        ts = self.times[skip:]
        return (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 2 else None


STORY = ("Write a long, detailed story (at least 1500 words) about a lighthouse keeper who repairs an old clock. "
         "Describe every step of the repair.")


def long_prompt(n_words, seed=7):
    import random
    rnd = random.Random(seed)
    words = "river stone lamp quiet orange field window paper engine silver garden harbor winter signal".split()
    body = " ".join(rnd.choice(words) for _ in range(n_words))
    return body + "\n\nCount how many times the word 'lamp' appears above, then answer in one short sentence."


class EngineLog:
    """The engine's per-request lines, read from the log after a byte offset: the prompt line (tokens reused and
    read, read time, generated tokens and time) and the decode expert cache hit rate line."""
    PROMPT = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms \(([\d.]+) tok/s\), "
                        r"(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)")
    HIT = re.compile(r"decode expert cache hit rate: ([\d.]+)% \((\d+) hits / (\d+) lookups\)")

    def __init__(self, path):
        self.path = path

    def mark(self):
        try:
            return os.path.getsize(self.path) if self.path else 0
        except OSError:
            return 0

    def text(self, off):
        if not self.path:
            return ""
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read().decode("utf-8", "replace")

    def request(self, off, timeout=10.0):
        """{prompt, reused, read, read_ms, read_tps, gen, gen_ms, gen_tps, hit, hits, lookups} of the first request
        whose lines follow off (it waits up to timeout for the hit-rate line), or {}."""
        end = time.time() + timeout
        while self.path:
            t = self.text(off)
            p, h = self.PROMPT.search(t), self.HIT.search(t)
            if p and h and h.start() > p.start():
                keys = ("prompt", "reused", "read", "read_ms", "read_tps", "gen", "gen_ms", "gen_tps")
                d = {k: float(v) if "." in v else int(v) for k, v in zip(keys, p.groups())}
                d.update(hit=float(h.group(1)), hits=int(h.group(2)), lookups=int(h.group(3)))
                return d
            if time.time() > end:
                break
            time.sleep(0.2)
        return {}


class Run:
    def __init__(self, args):
        self.a = args
        self.sw = Swap(args.model)
        self.res = []      # (check, pass/fail/info, text)
        self.log = []
        self.elog = EngineLog(args.engine_log)
        self.seed = time.time_ns() & 0xffffffff   # each long prompt differs from its first word: no cache reuse

    def note(self, s):
        print(s, flush=True)
        self.log.append(s)

    def rec(self, check, ok, text):
        self.res.append((check, "PASS" if ok is True else ("FAIL" if ok is False else str(ok)), text))
        self.note(f"[{check}] {'PASS' if ok is True else ('FAIL' if ok is False else ok)}: {text}")

    def cards(self):
        code, st = self.sw.state()
        return {c["gpu"]: c for c in st.get("gpus", [])}, st

    def seg(self, card):
        """The lend block size of the card: /vram/state's block_mib, else --segment-mib."""
        return self.cards()[0].get(card, {}).get("block_mib") or self.a.segment_mib

    def wait_lent_zero(self):
        for _ in range(100):
            _, st = self.sw.state()
            if not st.get("leases"):
                return st
            time.sleep(0.2)
        return st

    # ---- checks ----
    def c_state(self):
        code, st = self.sw.state()
        bus = sorted(norm(b) for b in self.smi.now())
        self.note(f"state {code}: {json.dumps(st)}")
        cards = sorted(c["gpu"] for c in st.get("gpus", []))
        self.rec("state", code == 200 and cards == bus and not st.get("leases"),
                 f"/vram/state gpus {cards}, engine GPUs from nvidia-smi {bus}")
        self.base = {c["gpu"]: c for c in st.get("gpus", [])}

    def c_big(self):
        """8 GiB lend and return on one card while one request streams: memory, timings, decode gap."""
        card, mib = self.a.card, self.a.big_mib
        before_state = self.cards()[0]
        s = Stream(self.sw.model, STORY, max_tokens=self.a.big_tokens)
        s.start()
        while len(s.times) < 30 and s.is_alive():
            time.sleep(0.05)
        time.sleep(2.0)
        pre = self.smi.now()
        code, body, t_send, t_ans = self.sw.lend(card, mib)
        self.note(f"lend {card} {mib}: {code} {body} in {t_ans - t_send:.3f} s")
        if code != 200:
            self.rec("lend-8g", False, f"lend answered {code} {body}")
            s.join()
            return
        lent_mib = body["mib"]
        t_free = self.smi.first_time(t_send, lambda b: pre.get(card, 0) - b.get(card, 0) >= mib)
        time.sleep(3.0)
        mid = self.smi.now()
        st_mid = self.cards()[0]
        code2, body2, r_send, r_ans = self.sw.give_back(body["id"])
        self.note(f"return {body['id']}: {code2} {body2} in {r_ans - r_send:.3f} s")
        t_back = self.smi.first_time(r_send, lambda b: b.get(card, 0) >= pre.get(card, 0) - 64)
        s.join()
        post = self.smi.now()
        after_state = self.cards()[0]
        gl, gr = s.max_gap(t_send, t_ans), s.max_gap(r_send, r_ans)
        blk = before_state[card].get("block_mib") if card in before_state else None
        self.note(f"engine MiB on {card}: before {pre.get(card)} lent {mid.get(card)} after {post.get(card)}; "
                  f"lent card state {st_mid.get(card)}")
        drop = pre.get(card, 0) - mid.get(card, 0)
        self.rec("lend-8g-size", code == 200 and mib <= lent_mib and (lent_mib - mib) < 1024,
                 f"asked {mib}, lease {lent_mib} MiB")
        self.rec("lend-8g-free", t_free is not None and t_free - t_ans <= 2.0 and drop >= mib,
                 f"engine process on {card} down {drop} MiB; driver showed >= {mib} MiB freed "
                 f"{(t_free - t_send) if t_free else float('nan'):.2f} s after send, "
                 f"{(t_free - t_ans) if t_free else float('nan'):.2f} s after the answer (lend call {t_ans - t_send:.2f} s)")
        self.rec("lend-8g-stall", gl is not None and gl <= 1.0, f"largest token gap across the lend {gl}")
        self.rec("return-8g", code2 == 200 and t_back is not None and t_back - r_ans <= 2.0
                 and abs(post.get(card, 0) - pre.get(card, 0)) <= 128,
                 f"return {code2} in {r_ans - r_send:.2f} s; back to {post.get(card)} MiB (before {pre.get(card)}), "
                 f"driver showed it {(t_back - r_send) if t_back else float('nan'):.2f} s after send")
        self.rec("return-8g-stall", gr is not None and gr <= 1.0, f"largest token gap across the return {gr}")
        same = card in after_state and after_state[card]["lent"] == 0 and \
            after_state[card]["lendable"] == before_state[card]["lendable"]
        self.rec("return-8g-state", same, f"state before {before_state.get(card)} after {after_state.get(card)}")
        self.rec("lend-8g-request", s.error is None and s.finish in ("stop", "length"),
                 f"request finish {s.finish} error {s.error}, {len(s.times)} deltas, decode {s.rate()} /s")
        ok = self.chat_ok()
        self.rec("after-return-chat", ok, "a chat request after the return answers")
        self.big = dict(card=card, mib=mib, lease=lent_mib, lend_s=t_ans - t_send, ret_s=r_ans - r_send,
                        free_s=(t_free - t_send) if t_free else None, back_s=(t_back - r_send) if t_back else None,
                        gap_lend=gl, gap_ret=gr, pre=pre.get(card), mid=mid.get(card), post=post.get(card))

    def chat_ok(self):
        r = requests.post(f"{SWAP}/v1/chat/completions", timeout=600, json={
            "model": self.sw.model, "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "What is 2+2? Answer with the number."}]})
        return r.status_code == 200 and "4" in json.dumps(r.json().get("choices", []))

    def c_each(self):
        """2048 MiB on each card with lendable >= 2048; the engine's memory on the other cards, and the whole memory
        of every GPU the engine does not use, move less than one block (/vram/state's block_mib, else --segment-mib)."""
        st, _ = self.cards()
        for card, c in sorted(st.items()):
            if c["lendable"] < 2048:
                code, body, *_ = self.sw.lend(card, 2048)
                self.rec(f"each-{card}", code == 409 and body.get("max_mib") == c["lendable"],
                         f"lendable {c['lendable']} < 2048: lend answered {code} {body}")
                continue
            seg = c.get("block_mib") or self.a.segment_mib
            pre, gpre = self.smi.now(), gpu_used()
            code, body, t0, t1 = self.sw.lend(card, 2048)
            time.sleep(1.5)
            mid, gmid = self.smi.now(), gpu_used()
            others = {b: mid.get(b, 0) - pre.get(b, 0) for b in pre if b != card}
            idle = {b: gmid.get(b, 0) - v for b, v in gpre.items() if b not in pre}
            code2, body2, *_ = self.sw.give_back(body.get("id")) if code == 200 else (None, None)
            time.sleep(1.0)
            post = self.smi.now()
            self.rec(f"each-{card}", code == 200 and code2 == 200 and pre.get(card, 0) - mid.get(card, 0) >= 2048
                     and all(abs(v) < seg for v in others.values()) and all(abs(v) < seg for v in idle.values()),
                     f"lend {code} {body} ({t1 - t0:.2f} s), this card down {pre.get(card, 0) - mid.get(card, 0)} MiB, "
                     f"others {others}, GPUs without the engine (whole GPU) {idle}, block {seg} MiB, return {code2}, "
                     f"back to {post.get(card)} (before {pre.get(card)})")

    def c_full(self):
        """Lend the card's full lendable, stream a request, return it, run a request: lendable after the return equals
        lendable before the lend."""
        card = self.a.card
        before = self.cards()[0][card]
        code, body, t0, t1 = self.sw.lend(card, before["lendable"])
        s = Stream(self.sw.model, STORY, max_tokens=400)
        s.start()
        s.join()
        code2 = self.sw.give_back(body["id"])[0] if code == 200 else None
        time.sleep(1.0)
        chat = self.chat_ok()
        after = self.cards()[0][card]
        self.rec("full-lendable", code == 200 and code2 == 200 and s.error is None and s.finish in ("stop", "length")
                 and chat and after["lendable"] == before["lendable"] and after["lent"] == 0,
                 f"lend of the full lendable {before['lendable']}: {code} {body} ({t1 - t0:.2f} s); request while lent: "
                 f"finish {s.finish} err {s.error}, {len(s.times)} deltas, decode {s.rate()} /s; return {code2}; "
                 f"chat after {chat}; state before {before} after {after}")

    def c_over(self):
        st, _ = self.cards()
        card = self.a.card
        lendable = st[card]["lendable"]
        pre = self.smi.now()
        ask = lendable + (st[card].get("block_mib") or self.a.segment_mib) + self.a.over_extra
        code, body, *_ = self.sw.lend(card, ask)
        post = self.smi.now()
        self.rec("over-lendable", code == 409 and body.get("max_mib") == lendable
                 and abs(post.get(card, 0) - pre.get(card, 0)) < 64,
                 f"lend of {ask} over {lendable}: {code} {body}; engine MiB {pre.get(card)} -> {post.get(card)}")

    def c_two(self):
        """Two leases of --two-mib on one card, the first returned first. After each step /vram/state lists exactly
        the open leases, the engine's memory on the card is down by their sum (within one block), and a short
        request streams; lendable at the end equals lendable at the start."""
        card, mib = self.a.card, self.a.two_mib
        seg = self.seg(card)
        st0 = self.cards()[0][card]
        pre = self.smi.now()
        open_, ids, steps_ok = {}, [], True
        for verb, name in (("lend", "A"), ("lend", "B"), ("return", "A"), ("return", "B")):
            if verb == "lend":
                code, body, t0, t1 = self.sw.lend(card, mib)
                if code == 200:
                    open_[name] = body
                    ids.append(body["id"])
            else:
                lease = open_.pop(name, None)
                code, body, t0, t1 = self.sw.give_back(lease["id"] if lease else None)
            time.sleep(1.0)
            _, st = self.sw.state()
            now = self.smi.now()
            lent = {c["gpu"]: c["lent"] for c in st.get("gpus", [])}.get(card)
            want = sum(l["mib"] for l in open_.values())
            listed = {l["id"] for l in st.get("leases", [])}
            drop = pre.get(card, 0) - now.get(card, 0)
            s = Stream(self.sw.model, "Name three rivers in Europe and one fact about each.", max_tokens=64)
            s.start()
            s.join()
            streamed = s.error is None and s.finish in ("stop", "length") and len(s.times) > 0
            ok = code == 200 and lent == want and listed == {l["id"] for l in open_.values()} \
                and abs(drop - want) < seg and streamed
            steps_ok = steps_ok and ok
            self.rec(f"two-{verb}-{name}", ok,
                     f"{verb} {name}: {code} {body} ({t1 - t0:.3f} s); state lent {lent} leases {sorted(listed)}, open "
                     f"{want} MiB; engine MiB on {card} {now.get(card)} (down {drop}, block {seg}); request after it: "
                     f"finish {s.finish} err {s.error}, {len(s.times)} deltas")
        st1 = self.cards()[0][card]
        self.rec("two-leases", steps_ok and len(set(ids)) == 2 and st1["lendable"] == st0["lendable"] and st1["lent"] == 0,
                 f"ids {ids}; lendable {st0['lendable']} at the start, {st1['lendable']} at the end, lent {st1['lent']}")
        for l in open_.values():
            self.sw.give_back(l["id"])

    def c_busy(self):
        """Lend 2048, a second process takes the memory, return -> 409 retry; free it, return -> 200."""
        card = self.a.card
        code, body, *_ = self.sw.lend(card, 2048)
        if code != 200:
            self.rec("busy-return", False, f"lend answered {code} {body}")
            return
        hog = subprocess.Popen([sys.executable, __file__, "--hog", card], stdout=subprocess.PIPE, text=True)
        line = hog.stdout.readline().strip()
        c1, b1, *_ = self.sw.give_back(body["id"])
        _, st = self.sw.state()
        still = [l["id"] for l in st.get("leases", [])]
        hog.terminate()
        hog.wait()
        time.sleep(0.5)
        c2, b2, *_ = self.sw.give_back(body["id"])
        self.rec("busy-return", c1 == 409 and body["id"] in still and c2 == 200,
                 f"hog: {line}; return while held {c1} {b1}, lease listed {body['id'] in still}; after free {c2} {b2}")

    def c_while_requests(self):
        """4096 MiB lend and return while a long prompt is read and a second request waits; both finish normally."""
        card = self.a.card
        a = Stream(self.sw.model, long_prompt(self.a.long_words, seed=int(time.time())), max_tokens=200)
        b = Stream(self.sw.model, STORY, max_tokens=300)
        a.start()
        time.sleep(0.5)
        b.start()
        time.sleep(self.a.read_wait)
        in_read = not a.times
        code, body, t0, t1 = self.sw.lend(card, 4096)
        self.note(f"lend during {'prompt read' if in_read else 'decode'}: {code} {body} in {t1 - t0:.2f} s")
        time.sleep(1.0)
        code2, body2, r0, r1 = self.sw.give_back(body.get("id")) if code == 200 else (None, None, 0, 0)
        a.join()
        b.join()
        ok = code == 200 and code2 == 200 and not a.error and not b.error and a.finish in ("stop", "length") \
            and b.finish in ("stop", "length")
        self.rec("lend-while-requests", ok,
                 f"lend {code} ({t1 - t0:.2f} s, sent {'during the prompt read' if in_read else 'after it'}), "
                 f"return {code2} ({r1 - r0:.2f} s); request A finish {a.finish} err {a.error} first token "
                 f"{a.first_token()}; B finish {b.finish} err {b.error}")
        self.rec("after-requests-chat", self.chat_ok(), "the engine keeps serving")

    def c_prompt(self):
        """Read a cold --prompt-words prompt with nothing lent, then with the full lendable of --prompt-card lent: the
        rate while lent is --prompt-ratio or more of the rate before the lend. Then two more reads, right after the
        return and after one --recover-tokens decode request (an engine that refills the returned slots at decode
        steps reads at the lent rate until then). With --engine-log the rates are the engine's own read rates, and
        each prompt reuses under 1% of its tokens."""
        card = self.a.prompt_card
        def read_rate(tag):
            self.seed += 1
            off = self.elog.mark()
            s = Stream(self.sw.model, long_prompt(self.a.prompt_words, seed=self.seed) + f" [{tag}]", max_tokens=8)
            s.start()
            s.join()
            e = self.elog.request(off)
            if e:
                return dict(n=e["prompt"], reused=e["reused"], secs=e["read_ms"] / 1000, rate=e["read_tps"], s=s)
            n, ft = (s.usage or {}).get("prompt_tokens"), s.first_token()
            return dict(n=n, reused=None, secs=ft, rate=(n / ft if n and ft else None), s=s)
        def txt(r):
            return f"{r['n']} tokens ({r['reused']} reused) in {r['secs'] or 0:.2f} s, {r['rate'] or 0:.0f} t/s"
        b0 = read_rate("base")
        st, _ = self.cards()
        full = st[card]["lendable"]
        off = self.elog.mark()
        code, body, *_ = self.sw.lend(card, full)
        l1 = read_rate("lent")
        log_tail = self.elog.text(off)
        tbt = [l for l in log_tail.splitlines() if "token by token" in l]
        code2 = self.sw.give_back(body["id"])[0] if code == 200 else None
        time.sleep(1.0)
        b1 = read_rate("after")
        d = Stream(self.sw.model, STORY, max_tokens=self.a.recover_tokens)
        d.start()
        d.join()
        b2 = read_rate("decoded")
        reads = (b0, l1, b1, b2)
        ratio = l1["rate"] / b0["rate"] if b0["rate"] and l1["rate"] else None
        cold = all(r["reused"] is None or (r["n"] and r["reused"] < 0.01 * r["n"]) for r in reads)
        self.rec("prompt-at-full-lend", code == 200 and code2 == 200 and ratio is not None
                 and ratio >= self.a.prompt_ratio and cold and not tbt and all(r["s"].error is None for r in reads),
                 f"{card} lendable {full}: lend {code} {body}, return {code2}; nothing lent {txt(b0)}; "
                 f"{full} MiB lent {txt(l1)}; after the return {txt(b1)}; after a {len(d.times)}-token decode request "
                 f"{txt(b2)}; lent / nothing lent "
                 f"{ratio if ratio is None else round(ratio, 3)} (need {self.a.prompt_ratio}); cold {cold}; "
                 f"token-by-token lines while lent: {len(tbt)}")

    def c_cost(self):
        """Decode t/s over a request set with nothing lent and with --cost-mib lent on --card."""
        prompts = [f"Explain topic {i} of computer architecture in about 300 words." for i in range(self.a.cost_n)]
        def run_set():
            rates = []
            for p in prompts:
                s = Stream(self.sw.model, p, max_tokens=400)
                s.start()
                s.join()
                if s.rate():
                    rates.append(s.rate())
            return sum(rates) / len(rates) if rates else None, rates
        base, br = run_set()
        code, body, *_ = self.sw.lend(self.a.card, self.a.cost_mib)
        lent, lr = run_set()
        if code == 200:
            self.sw.give_back(body["id"])
        self.rec("decode-cost", "info", f"decode over {len(prompts)} requests: nothing lent {base:.2f} t/s, "
                 f"{self.a.cost_mib} MiB lent on {self.a.card} ({code}) {lent:.2f} t/s "
                 f"({(lent / base - 1) * 100:+.1f}%); per request {[round(x, 1) for x in br]} / "
                 f"{[round(x, 1) for x in lr]}")

    def c_cost3(self):
        """Decode t/s over a request set before a lend, while --cost-mib is lent on --card, and after the return."""
        prompts = [f"Explain topic {i} of computer architecture in about 300 words." for i in range(self.a.cost_n)]
        def run_set():
            rates = []
            for p in prompts:
                s = Stream(self.sw.model, p, max_tokens=400)
                s.start()
                s.join()
                if s.rate():
                    rates.append(s.rate())
            return (sum(rates) / len(rates) if rates else 0.0), [round(x, 1) for x in rates]
        for i in range(self.a.warm):
            w = Stream(self.sw.model, f"Warm-up {i}: describe a bridge in about 200 words.", max_tokens=300)
            w.start()
            w.join()
        base, br = run_set()
        code, body, *_ = self.sw.lend(self.a.card, self.a.cost_mib)
        lent, lr = run_set()
        rc = self.sw.give_back(body["id"])[0] if code == 200 else None
        after, ar = run_set()
        self.rec("decode-cost-3", "info", f"decode over {len(prompts)} requests: before {base:.2f} t/s {br}; "
                 f"{self.a.cost_mib} MiB lent on {self.a.card} ({code}) {lent:.2f} t/s {lr}; after the return ({rc}) "
                 f"{after:.2f} t/s {ar}")

    def c_recover(self):
        """Decode t/s and the engine's decode hit rate on one prompt (--recover-tokens each), --cost-n runs before a
        --cost-mib lend on --card, --cost-n while it is lent, then one run during which the lease is returned (its
        largest token gap is the return's stall), then --cost-n after the return. The hit rate is back when a run that
        ends within 2000 decoded tokens after the return is within 1 point of the mean before the lend. Needs
        --engine-log for the hit rate."""
        card, mib, n = self.a.card, self.a.cost_mib, self.a.cost_n
        def one(tag, lease=None):
            off = self.elog.mark()
            s = Stream(self.sw.model, STORY, max_tokens=self.a.recover_tokens)
            s.start()
            r = dict(tag=tag)
            if lease:
                while len(s.times) < 30 and s.is_alive():
                    time.sleep(0.05)
                code, body, r_send, r_ans = self.sw.give_back(lease)
                r.update(ret=code, ret_s=round(r_ans - r_send, 3))
            s.join()
            e = self.elog.request(off)
            r.update(gen=e.get("gen"), tps=e.get("gen_tps"), hit=e.get("hit"), stream_tps=s.rate() and round(s.rate(), 1),
                     finish=s.finish, err=s.error)
            if lease:
                r.update(gap=s.max_gap(r_send, r_ans), after=len([t for t in s.times if t > r_ans]))
            self.note(f"recover {card} {tag}: {r}")
            return r
        def mean(rs, k):
            v = [r[k] for r in rs if r.get(k) is not None]
            return round(sum(v) / len(v), 2) if v else None
        for i in range(self.a.warm):
            one(f"warm-{i}")
        st0 = self.cards()[0][card]
        before = [one(f"before-{i}") for i in range(n)]
        code, body, *_ = self.sw.lend(card, mib)
        if code != 200:
            self.rec("recover", False, f"lend answered {code} {body}")
            return
        during = [one(f"lent-{i}") for i in range(n)]
        ret = one("return", lease=body["id"])
        after = [one(f"after-{i}") for i in range(n)]
        time.sleep(1.0)
        st1 = self.cards()[0][card]
        h0 = mean(before, "hit")
        ends, k = [], ret.get("after") or 0
        ends.append((k, ret.get("hit")))
        for r in after:
            k += r.get("gen") or 0
            ends.append((k, r.get("hit")))
        back = next((k for k, h in ends if h is not None and h0 is not None and k <= 2000 and h >= h0 - 1.0), None)
        def ph(rs):
            return (f"engine {mean(rs, 'tps')} t/s {[r['tps'] for r in rs]}, stream {mean(rs, 'stream_tps')} t/s, "
                    f"hit {mean(rs, 'hit')}% {[r['hit'] for r in rs]}")
        self.rec("recover-decode", "info", f"{card}, {mib} MiB lent ({code}, lease {body.get('mib')} MiB), "
                 f"{self.a.recover_tokens} tokens per run: before {ph(before)}; lent {ph(during)}; return run "
                 f"engine {ret['tps']} t/s hit {ret['hit']}%; after {ph(after)}")
        self.rec("recover-return-stall", ret.get("ret") == 200 and ret.get("gap") is not None and ret["gap"] <= 1.0,
                 f"return {ret.get('ret')} in {ret.get('ret_s')} s while a request streamed; largest token gap "
                 f"across it {ret.get('gap')} s")
        self.rec("recover-hit-rate", back is not None,
                 f"hit rate before {h0}%; after the return (decoded tokens since the return, hit rate of the run "
                 f"ending there) {ends}; back within 1 point by token {back}")
        self.rec("recover-state", st1["lent"] == 0 and st1["lendable"] == st0["lendable"]
                 and all(r["err"] is None and r["finish"] in ("stop", "length") for r in before + during + [ret] + after),
                 f"state before {st0} after {st1}; every request finished without error")

    def c_late(self):
        """A lend sent during a prompt read longer than the server's 30 s wait: 503, /vram/state lists the late lease
        until the server has given it back by itself, then nothing stays lent. /vram/state is read before the lend
        and every 0.5 s after its answer, and the engine's VRAM Lend lines are kept, so an engine that answers the
        lend within the wait is recorded as it happened."""
        card = self.a.card
        self.seed += 1
        off = self.elog.mark()
        _, st0 = self.sw.state()
        a = Stream(self.sw.model, long_prompt(self.a.late_words, seed=self.seed), max_tokens=50)
        a.start()
        time.sleep(self.a.read_wait)
        in_read = not a.times
        code, body, t0, t1 = self.sw.lend(card, 2048)
        answered_in_read = not a.times
        seen = []   # (s after the answer, lease ids, lent on the card)
        while time.time() - t1 < 120:
            _, st = self.sw.state()
            ids = [l["id"] for l in st.get("leases", [])]
            lent = {c["gpu"]: c["lent"] for c in st.get("gpus", [])}.get(card)
            if not seen or seen[-1][1:] != (ids, lent):
                seen.append((round(time.time() - t1, 1), ids, lent))
            if not a.is_alive() and (code == 200 or not ids):
                break
            time.sleep(0.5)
        a.join()
        if code == 200:
            self.sw.give_back(body["id"])
        time.sleep(3)
        _, st = self.sw.state()
        lent = {c["gpu"]: c["lent"] for c in st.get("gpus", [])}
        e = self.elog.request(off)
        lines = [l.strip() for l in self.elog.text(off).splitlines() if "VRAM Lend" in l]
        for l in lines:
            self.note(f"engine: {l}")
        self.rec("late-lend", code == 503 and in_read and not st.get("leases") and all(v == 0 for v in lent.values())
                 and a.error is None and a.finish in ("stop", "length"),
                 f"lend sent {t0 - a.t0:.1f} s into the request ({'during' if in_read else 'after'} the prompt read) "
                 f"answered {code} {body} after {t1 - t0:.1f} s ({'during' if answered_in_read else 'after'} the "
                 f"read); prompt {e.get('prompt')} tokens ({e.get('reused')} reused) read in "
                 f"{e.get('read_ms', 0) / 1000:.1f} s; request finish {a.finish} err {a.error} first token "
                 f"{a.first_token()}; state before {json.dumps(st0)}; leases and lent on {card} after the answer "
                 f"(s, ids, MiB) {seen}; state at the end {json.dumps(st)}; engine VRAM Lend lines {len(lines)}")
        self.rec("after-late-chat", self.chat_ok(), "the engine keeps serving")

    def c_restart(self):
        card = self.a.card
        code, body, *_ = self.sw.lend(card, 1024)
        if self.a.restart_url:   # the engine restarts and the server keeps running
            r = requests.post(self.a.restart_url.format(base=SWAP, model=self.sw.model), json={}, timeout=300)
            how = f"engine stop {r.status_code} {r.text[:120]}"
        else:
            self.sw.unload()
            how = "server unloaded"
        time.sleep(3)
        lc, lt, _ = self.sw.load()
        self.smi.pids = engine_pids()
        _, st = self.sw.state()
        c_old, b_old, *_ = self.sw.give_back(body.get("id"))
        c_new, b_new, *_ = self.sw.lend(card, 1024)
        if c_new == 200:
            self.sw.give_back(b_new["id"])
        ok = code == 200 and lc == 200 and not st.get("leases") and all(c["lent"] == 0 for c in st["gpus"]) \
            and c_old == 404
        self.rec("restart-clears", ok, f"lend {code} {body}; {how}; reload {lc} in {lt:.0f} s; state after "
                 f"{json.dumps(st)}; return of the old id {c_old} {b_old}; a new lend {c_new} {b_new}")

    def main(self):
        self.note(f"# {self.a.model} {time.strftime('%Y-%m-%d %H:%M:%S')}")
        code, dt, _ = self.sw.load()
        self.note(f"load/answer {code} in {dt:.0f} s")
        self.smi = Smi()
        self.smi.pids = engine_pids()
        self.note(f"engine pids {self.smi.pids}")
        time.sleep(1)
        for c in self.a.checks.split(","):
            try:
                getattr(self, "c_" + c)()
            except Exception as e:   # noqa: BLE001 - a crashed check is a failed check
                self.rec(c, False, f"check raised {e!r}")
            self.wait_lent_zero()
        self.smi.stop()
        with open(self.a.out, "a") as f:
            f.write(f"\n## {self.a.model} {time.strftime('%Y-%m-%d %H:%M:%S')} checks {self.a.checks}\n\n")
            f.write("| check | result | detail |\n|---|---|---|\n")
            for c, r, t in self.res:
                f.write(f"| {c} | {r} | {t.replace('|', '/')} |\n")
            f.write("\n<details><summary>log</summary>\n\n```\n" + "\n".join(self.log) + "\n```\n</details>\n")


def hog(card):
    """Hold all but ~256 MiB of the card's free memory until killed (a borrower that keeps the memory)."""
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    rt = ctypes.CDLL(os.environ.get("CUDART", "/usr/local/cuda/lib64/libcudart.so"))
    dev = ctypes.c_int()
    rt.cudaDeviceGetByPCIBusId(ctypes.byref(dev), card.encode())
    rt.cudaSetDevice(dev)
    free, total = ctypes.c_size_t(), ctypes.c_size_t()
    rt.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total))
    want = max(0, free.value - 256 * MIB)
    p = ctypes.c_void_p()
    err = rt.cudaMalloc(ctypes.byref(p), ctypes.c_size_t(want))
    print(f"dev {dev.value} held {want >> 20} MiB (err {err}, free was {free.value >> 20})", flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--hog":
        hog(sys.argv[2])
        sys.exit(0)
    ap = argparse.ArgumentParser(description="Live checks of VRAM Lend on a served model.")
    ap.add_argument("model", help="the model id the server serves")
    ap.add_argument("out", help="markdown file the results are appended to")
    ap.add_argument("--base-url", default=SWAP, help="OpenAI-compatible server (default: %(default)s)")
    ap.add_argument("--vram-url", default=VRAM_URL, help="where /vram/* is served, {base} and {model} filled in")
    ap.add_argument("--unload-url", default=UNLOAD_URL, help="POST here unloads the model (restart check)")
    ap.add_argument("--engine-match", default=ENGINE_MATCH, help="regex on pgrep -af lines of the engine's processes")
    ap.add_argument("--checks", default="state,big,each,over,two,busy,while_requests,restart")
    ap.add_argument("--gpu", "--card", dest="card", default="0000:09:00.0", help="PCI address of the GPU to lend")
    ap.add_argument("--big-mib", type=int, default=8192)
    ap.add_argument("--big-tokens", type=int, default=1500)
    ap.add_argument("--prompt-gpu", "--prompt-card", dest="prompt_card", default="0000:05:00.0",
                    help="PCI address for the prompt check")
    ap.add_argument("--prompt-words", type=int, default=3000)
    ap.add_argument("--prompt-ratio", type=float, default=0.5,
                    help="the prompt check passes when the rate with the full lendable lent is this share or more of "
                         "the rate with nothing lent (default %(default)s)")
    ap.add_argument("--long-words", type=int, default=12000)
    ap.add_argument("--read-wait", type=float, default=2.0)
    ap.add_argument("--engine-log", default="", help="the engine's log file (prompt and late checks read it)")
    ap.add_argument("--restart-url", default="",
                    help="POST here (JSON {}) stops the engine and the server keeps running; the reload then starts a "
                         "new engine process under the same server. {base} and {model} filled in. Default: the "
                         "restart check unloads the whole server with --unload-url")
    ap.add_argument("--segment-mib", type=int, default=64,
                    help="lend block size when /vram/state gives no block_mib (default %(default)s)")
    ap.add_argument("--over-extra", type=int, default=1024,
                    help="the over-lendable lend asks lendable + one block + this many MiB (default %(default)s)")
    ap.add_argument("--two-mib", type=int, default=1024, help="size of each lease in the two-lease check")
    ap.add_argument("--cost-mib", type=int, default=8192)
    ap.add_argument("--cost-n", type=int, default=8)
    ap.add_argument("--warm", type=int, default=0)
    ap.add_argument("--recover-tokens", type=int, default=1000,
                    help="tokens per request in the recover check (default %(default)s)")
    ap.add_argument("--late-words", type=int, default=80000,
                    help="prompt length in words for the late check. The prompt read must take longer than the "
                         "server's wait (30 s), or an engine that answers lends only after the read answers in time and "
                         "the check fails; an engine that answers lends during the read never reaches the late case "
                         "(default %(default)s)")
    args = ap.parse_args()
    SWAP, VRAM_URL, UNLOAD_URL, ENGINE_MATCH = args.base_url, args.vram_url, args.unload_url, args.engine_match
    Run(args).main()

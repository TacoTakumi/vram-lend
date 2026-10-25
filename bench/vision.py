#!/usr/bin/env python3
"""Vision smoke on VRAM Lend: one picture request while /vram/state is polled every 100 ms.

The engine's vision encoder borrows its memory as a lease on one GPU during the encode and returns it after. The
check passes when the answer holds the expected text, exactly one lease on --gpu is seen during the request, and no
lease is left after the answer. The results are appended to <out.md> as a markdown table.

Usage: vision.py <model id> <out.md> --image <png> --expect <text> [--gpu 0000:01:00.0] [--base-url ...]
"""
import argparse, base64, json, os, threading, time
import requests

ap = argparse.ArgumentParser(description="Vision smoke on VRAM Lend.")
ap.add_argument("model", help="the model id the server serves")
ap.add_argument("out", help="markdown file the results are appended to")
ap.add_argument("--image", required=True, help="a PNG with a short text in it")
ap.add_argument("--expect", required=True, help="text the answer must contain")
ap.add_argument("--gpu", default="0000:01:00.0", help="PCI address the encoder's lease is expected on")
ap.add_argument("--base-url", default=os.environ.get("VRAM_LEND_BASE_URL", "http://127.0.0.1:8080"),
                help="OpenAI-compatible server (default: %(default)s)")
ap.add_argument("--vram-url", default="{base}/upstream/{model}", help="where /vram/* is served")
a = ap.parse_args()

chat = f"{a.base_url}/v1/chat/completions"
up = a.vram_url.format(base=a.base_url, model=a.model)
requests.post(chat, timeout=900, json={
    "model": a.model, "max_tokens": 8, "messages": [{"role": "user", "content": "Say OK."}]})

polls, done = [], threading.Event()


def poll():
    while not done.is_set():
        try:
            st = requests.get(up + "/vram/state", timeout=5).json()
            polls.append((time.time(), st.get("leases", []), {c["gpu"]: c["lent"] for c in st.get("gpus", [])}))
        except Exception as e:   # noqa: BLE001 - a missed poll is recorded, not fatal
            polls.append((time.time(), None, repr(e)))
        time.sleep(0.1)


threading.Thread(target=poll, daemon=True).start()
time.sleep(0.5)
with open(a.image, "rb") as f:
    img = base64.b64encode(f.read()).decode()
t0 = time.time()
r = requests.post(chat, timeout=900, json={
    "model": a.model, "temperature": 0, "max_tokens": 600, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What exact text is written in this image? Reply with the text only."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]}]})
t1 = time.time()
time.sleep(3)
done.set()
text = r.json()["choices"][0]["message"].get("content") if r.status_code == 200 else r.text[:300]
with_lease = [(t - t0, l) for t, l, _ in polls if l]
first = with_lease[0][0] if with_lease else None
last = with_lease[-1][0] if with_lease else None
after = [l for t, l, _ in polls if t > t1 + 0.5]
lease = with_lease[0][1] if with_lease else None
ok_text = r.status_code == 200 and a.expect in (text or "")
ok_lease = bool(lease) and len(lease) == 1 and lease[0]["gpu"] == a.gpu
ok_after = bool(after) and all(l == [] for l in after)
with open(a.out, "a") as f:
    f.write(f"\n## {a.model} {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n| check | result | detail |\n|---|---|---|\n")
    f.write(f"| picture read | {'PASS' if ok_text else 'FAIL'} | HTTP {r.status_code}, answer {text!r}, {t1 - t0:.1f} s |\n")
    f.write(f"| lease during encode | {'PASS' if ok_lease else 'FAIL'} | {lease} seen from {first} s to {last} s after the "
            f"request ({len(with_lease)} of {len(polls)} polls at 100 ms) |\n")
    f.write(f"| no lease after | {'PASS' if ok_after else 'FAIL'} | {len(after)} polls after the answer, leases "
            f"{sorted(set(json.dumps(x) for x in after))} |\n")
with open(a.out) as f:
    print(f.read()[-1200:])

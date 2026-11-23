"""Jo-box app v5.1: triple watcher — FLOP, RackNerd, Delphi (24/7).

- FLOP: GET https://flop.finance/testnet/ every 2 min (1 min in the
  launch window 24 Nov 12:00-18:00 WIB); alert once when a marker
  (faucet/explorer/setup/claim/live) first appears or "draft" goes away.
- RackNerd: poll both LowEndTalk threads every 2 min; alert only on a
  NEW post by dustinc announcing a giveaway (giveaway / next 5 / next 10).
  Comment text is extracted first — never raw body hash (dynamic content
  false-positives). Cloudflare 403/429 means "unknown", never "no round";
  back off 10 min. VM cron stays authoritative for RackNerd.
- Delphi: GET https://competition.delphi.fyi/markets.json every 5 min;
  alert once when it answers 200 with real JSON (currently 404).
Alert sinks: Discord webhook (DISCORD_WEBHOOK env, once wired),
HTTP /status on the allocation port (Jo polls it), console + state.json.
Self-update: hourly `git pull`; if app.py changed and compiles, reload.
The panel console is NOT a shell (input goes to stdin).
"""
import hashlib
import html.parser
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = "https://github.com/tiVan0/nodaroo-jo-box"
ALLOC_PORT = 25753  # se2.nodaroo.fun:25753
VERSION = "v5.1"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

CONFIG = {
    "delphi": {"url": "https://competition.delphi.fyi/markets.json", "interval": 300},
    "flop": {
        "url": "https://flop.finance/testnet/",
        "markers": ["faucet", "explorer", "setup", "claim", "live"],
    },
    "racknerd": {
        "bf": {"base": "https://lowendtalk.com/discussion/223558/racknerd-s-black-friday-takeover-new-deals-new-tech-adminbolt-hosting-hundreds-of-giveaways", "page": 296},
        "main": {"base": "https://lowendtalk.com/discussion/221872/giveaway-by-adminbolt-racknerd-100x-free-vps-for-a-year-20x-lifetime-licenses-lets-talk", "page": 172},
        "interval": 120,
    },
}

STATE_FILE = "state.json"
_lock = threading.Lock()
state = {
    "version": VERSION,
    "started": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    "watchers": {},
    "flop": {"markers": [], "draft": None},
    "racknerd": {"bf_page": 296, "main_page": 172, "seen": []},
    "alerts": [],
}
try:
    with open(STATE_FILE) as f:
        old = json.load(f)
    for k in ("flop", "racknerd", "alerts"):
        if k in old:
            state[k] = old[k]
except Exception:
    pass


def save_state():
    try:
        with _lock:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=1)
    except Exception as e:
        print(f"STATE FAIL: {e}", flush=True)


def log(msg, flush=True):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=flush)


def alert(source, msg):
    line = f"ALERT [{source}] {msg}"
    log(line)
    with _lock:
        state["alerts"].append({"at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "source": source, "msg": msg})
        state["alerts"] = state["alerts"][-50:]
    save_state()
    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"content": line}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"WEBHOOK OK {r.status}", flush=True)
        except Exception as e:
            log(f"WEBHOOK FAIL: {type(e).__name__}: {e}", flush=True)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def set_watcher(name, note):
    with _lock:
        state["watchers"][name] = {"last": time.strftime("%H:%M:%S"), "note": note}
    save_state()


def watch_delphi(cfg):
    was_live = False
    while True:
        try:
            status, body = fetch(cfg["url"])
            live = status == 200 and len(body) > 100
            note = f"HTTP {status}, {len(body)} bytes"
            if live and not was_live:
                alert("delphi", f"DELPHI MARKETS LIVE! {cfg['url']} menjawab 200 ({len(body)} bytes) — kompetisi buka!")
            was_live = live
        except urllib.error.HTTPError as e:
            note = f"HTTP {e.code} (belum buka)"
            was_live = False
        except Exception as e:
            note = f"ERR {type(e).__name__}: {e}"
        set_watcher("delphi", note)
        time.sleep(cfg["interval"])


def flop_interval():
    now = time.gmtime()
    if now.tm_mon == 11 and now.tm_mday == 24 and 5 <= now.tm_hour < 11:
        return 60  # launch window 12:00-18:00 WIB
    return 120


def watch_flop(cfg):
    while True:
        try:
            status, body = fetch(cfg["url"])
            text = body.decode("utf-8", "ignore").lower()
            found = sorted({m for m in cfg["markers"] if m in text})
            draft = ("draft" in text) or ("opens q4 2026" in text)
            note = f"HTTP {status}, markers={found or 'none'}, draft={draft}"
            prev = state["flop"]
            if prev["draft"] is None:
                pass  # baseline first run: no alert
            else:
                for m in found:
                    if m not in prev["markers"]:
                        alert("flop", f"FLOP: marker baru muncul: '{m}' di {cfg['url']}")
                if prev["draft"] and not draft:
                    alert("flop", f"FLOP: status 'draft' HILANG dari {cfg['url']} — testnet kemungkinan buka!")
            with _lock:
                state["flop"] = {"markers": found, "draft": draft}
            set_watcher("flop", note)
            wait = flop_interval()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                set_watcher("flop", f"CLOUDFLARE {e.code} — unknown, backoff 10 mnt")
                wait = 600
            else:
                set_watcher("flop", f"HTTP {e.code}")
                wait = 120
        except Exception as e:
            set_watcher("flop", f"ERR {type(e).__name__}: {e}")
            wait = 120
        time.sleep(wait)


class LETParser(html.parser.HTMLParser):
    """Extract (author, text) pairs from LowEndTalk comment HTML."""

    def __init__(self):
        super().__init__()
        self.comments = []
        self._author = None
        self._in_user = False
        self._in_msg = False
        self._depth = 0
        self._buf = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if tag == "a" and "Username" in cls:
            self._in_user = True
        if tag == "div" and "Message" in cls:
            if not self._in_msg:
                self._in_msg = True
                self._depth = 1
                self._buf = []
            else:
                self._depth += 1
        elif self._in_msg and tag == "div":
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "a" and self._in_user:
            self._in_user = False
        if tag == "div" and self._in_msg:
            self._depth -= 1
            if self._depth <= 0:
                self._in_msg = False
                text = " ".join("".join(self._buf).split())
                if self._author and text:
                    self.comments.append((self._author, text))
                self._buf = []

    def handle_data(self, data):
        if self._in_user:
            self._author = data.strip()
        if self._in_msg:
            self._buf.append(data)


def let_comments(url):
    status, body = fetch(url)
    p = LETParser()
    p.feed(body.decode("utf-8", "ignore"))
    return status, p.comments


def watch_racknerd(cfg):
    giveaway_kw = ("giveaway", "next 5", "next 10")
    while True:
        try:
            for key in ("bf", "main"):
                t = cfg[key]
                base, page = t["base"], state["racknerd"][f"{key}_page"]
                for pno in (page, page + 1):
                    url = f"{base}/p{pno}"
                    try:
                        status, comments = let_comments(url)
                    except urllib.error.HTTPError as e:
                        if e.code in (403, 429):
                            set_watcher("racknerd", f"CLOUDFLARE {e.code} — unknown, backoff 10 mnt")
                            raise _Backoff
                        if e.code == 404:
                            continue  # next page does not exist yet
                        raise
                    if pno == page + 1 and comments:
                        with _lock:
                            state["racknerd"][f"{key}_page"] = pno
                        log(f"RACKNERD thread {key} maju ke p{pno}", flush=True)
                    seen = set(state["racknerd"]["seen"])
                    baseline = not seen
                    for author, text in comments:
                        k = hashlib.sha256(f"{author}|{text[:200]}".encode()).hexdigest()[:16]
                        if k in seen:
                            continue
                        seen.add(k)
                        low = text.lower()
                        if not baseline and author.lower() == "dustinc" and any(w in low for w in giveaway_kw):
                            alert("racknerd", f"RONDE BARU? dustinc di {key} p{pno}: {text[:200]} — {url}")
                    with _lock:
                        state["racknerd"]["seen"] = sorted(seen)[-500:]
                set_watcher("racknerd", f"bf p{state['racknerd']['bf_page']}, main p{state['racknerd']['main_page']}, seen {len(state['racknerd']['seen'])}")
            wait = cfg["interval"]
        except _Backoff:
            wait = 600
        except Exception as e:
            set_watcher("racknerd", f"ERR {type(e).__name__}: {e}")
            wait = 120
        time.sleep(wait)


class _Backoff(Exception):
    pass


def self_update_loop():
    while True:
        time.sleep(3600)
        try:
            r = subprocess.run(["git", "pull"], timeout=120, capture_output=True, text=True)
            out = (r.stdout + r.stderr).strip()
            log(f"SELF-UPDATE pull rc={r.returncode}: {out[:200]}", flush=True)
            if r.returncode == 0 and "Already up to date" not in out:
                c = subprocess.run([sys.executable, "-m", "py_compile", "app.py"], timeout=60, capture_output=True, text=True)
                if c.returncode == 0:
                    log("SELF-UPDATE: app.py baru OK — reload", flush=True)
                    save_state()
                    os.execv(sys.executable, [sys.executable, "app.py"])
                else:
                    log(f"SELF-UPDATE: compile gagal, tidak reload: {c.stderr[:200]}", flush=True)
        except Exception as e:
            log(f"SELF-UPDATE FAIL: {type(e).__name__}: {e}", flush=True)


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            payload = json.dumps(state, indent=1).encode()
        if self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            payload = f"Jo-box {VERSION} alive\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def serve_status():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", ALLOC_PORT), StatusHandler)
        log(f"STATUS server listen 0.0.0.0:{ALLOC_PORT}", flush=True)
        srv.serve_forever()
    except Exception as e:
        log(f"STATUS server FAIL: {type(e).__name__}: {e}", flush=True)


def run(cmd):
    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip().replace("\n", " | ")[:300]
        print(f"BOOT {'OK' if r.returncode == 0 else 'FAIL'} {' '.join(cmd)} -> {out}", flush=True)
        return r.returncode == 0
    except Exception as e:
        print(f"BOOT FAIL {' '.join(cmd)} -> {type(e).__name__}: {e}", flush=True)
        return False


print(f"=== Jo-box test {VERSION} — triple watcher ===", flush=True)
print("python:", sys.version.split()[0], flush=True)
print("platform:", platform.platform(), flush=True)
print("hostname:", socket.gethostname(), flush=True)
print("cpu count:", os.cpu_count(), flush=True)
print("cwd:", os.getcwd(), flush=True)
print("files:", sorted(os.listdir(".")), flush=True)

git = shutil.which("git")
print("git:", git or "MISSING", flush=True)
if git and not os.path.isdir(".git"):
    print("BOOT: no .git — linking this folder to the repo...", flush=True)
    ok = (
        run(["git", "init", "-b", "main"])
        and run(["git", "remote", "add", "origin", REPO])
        and run(["git", "fetch", "origin"])
        and run(["git", "reset", "--hard", "origin/main"])
        and run(["git", "branch", "--set-upstream-to=origin/main", "main"])
        and run(["git", "status", "--short", "--branch"])
    )
    print("BOOT: CLONE OK — folder is now a git checkout" if ok else "BOOT: CLONE FAIL — see BOOT lines above", flush=True)
    print("files after boot:", sorted(os.listdir(".")), flush=True)
elif os.path.isdir(".git"):
    print("BOOT: .git already present — already a checkout", flush=True)

for url in ("https://ifconfig.me/ip", "https://api.github.com/"):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            print(f"EGRESS OK {url} -> {r.status} {r.read(120)!r}", flush=True)
    except Exception as e:
        print(f"EGRESS FAIL {url} -> {type(e).__name__}: {e}", flush=True)

threading.Thread(target=serve_status, daemon=True).start()
threading.Thread(target=self_update_loop, daemon=True).start()
threading.Thread(target=watch_delphi, args=(CONFIG["delphi"],), daemon=True).start()
threading.Thread(target=watch_flop, args=(CONFIG["flop"],), daemon=True).start()
threading.Thread(target=watch_racknerd, args=(CONFIG["racknerd"],), daemon=True).start()
log("WATCHERS started: delphi + flop + racknerd", flush=True)
save_state()

n = 0
while True:
    n += 1
    print(f"heartbeat {n} - Jo-box hidup {n * 30} detik [{VERSION} triple watcher]", flush=True)
    time.sleep(30)

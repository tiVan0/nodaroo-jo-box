"""Jo-box app v5.4.2: triple watcher + radar — FLOP, RackNerd, Delphi,
flop_agent, bounty_radar, testnet_radar (24/7).

- bounty_radar: every 60 min — NEW free bug-bounty/audit programs
  (Sherlock API JSON, Cantina embedded JSON, Code4rena tiles).
  Immunefi NOT readable (JS-only list) — skipped, see radar section.
- testnet_radar: every 60 min — NEW incentivized testnets
  (airdrops.io/latest filtered by "testnet", 99airdrops testnet list,
  airdropalert.com/testnet-airdrops as backup leg — airdrops.io 403s
  from cloud egress IPs, verified 24 Nov 2026).
  Alert only for items never seen before (seen-set in state.json).
  Radar source errors alert Discord ONCE on OK->ERR and ERR->OK
  transitions (state["radar_errors"]); no spam while an error persists.

- FLOP: GET https://flop.finance/testnet/ every 2 min (1 min in the
  launch window 24 Nov 12:00-18:00 WIB); alert once when a marker
  (faucet/explorer/setup/claim/live) first appears or "draft" goes away.
- RackNerd: poll both LowEndTalk threads every 2 min; alert only on a
  NEW post by dustinc announcing a round: his own words (quotes stripped)
  with "first/next N" + (eligible|screenshot|photo|"shall receive");
  bandwidth confirmations never match (fixed 25 Nov 2026 — a quoted
  member "giveaway" caused false alerts on p404/p414).
  Comment text is extracted first — never raw body hash (dynamic content
  false-positives). Cloudflare 403/429 means "unknown", never "no round";
  back off 10 min. VM cron stays authoritative for RackNerd.
- Delphi: GET https://competition.delphi.fyi/markets.json every 5 min;
  alert once when it answers 200 with real JSON (currently 404).
- flop_agent: state machine DORMANT -> READY -> WAITING_FAUCET ->
  ACTIVE -> PAUSED. While the testnet is closed it only reads the
  open-markers watch_flop already tracks (state["flop"]) and logs.
  Faucet/inference/signing integration points are TODO placeholders
  (FAUCET_URL, INFERENCE_URL, sign_request) — no keys are ever read.
Alert sinks: Discord webhook (DISCORD_WEBHOOK env, or config.json in the
private repo tiVan0/jo-box-secrets, cloned at boot with USERNAME +
ACCESS_TOKEN — the token is never logged or kept on disk),
HTTP /status on the allocation port (Jo polls it), console + state.json.
Self-update: hourly `git pull`; if app.py changed and compiles, reload.
The panel console is NOT a shell (input goes to stdin).
"""
import hashlib
import html.parser
import json
import os
import platform
import re
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
VERSION = "v5.4.2"
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
    "bounty_radar": {"interval": 3600},
    "testnet_radar": {"interval": 3600},
}

STATE_FILE = "state.json"

# --- FLOP agent scaffold (v5.3.0) -------------------------------------------
# Titik integrasi — TODO: isi setelah riset SDK FLOP selesai.
# JANGAN implementasikan signing di sini, JANGAN baca kunci/seed apa pun
# (mis. ~/workspace/flop/identity.seed) — agen tidak menyentuh kredensial.
FAUCET_URL = ""       # TODO: URL faucet testnet FLOP (dari riset SDK)
INFERENCE_URL = ""    # TODO: endpoint inference FLOP / flop-sdk (dari riset SDK)
FLOP_SDK = ""         # TODO: nama + versi SDK resmi (mis. flop-sdk) dan cara
                      # memanggilnya dari Python stdlib (dari riset SDK)
_lock = threading.Lock()
state = {
    "version": VERSION,
    "started": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    "watchers": {},
    "flop": {"markers": [], "draft": None},
    "racknerd": {"bf_page": 296, "main_page": 172, "seen": []},
    "flop_agent": {"status": "DORMANT", "testnet_open": False, "faucet_url": "", "inference_endpoint": "", "last_check": None},
    "bounty_radar": {"seen": []},
    "testnet_radar": {"seen": []},
    "radar_errors": {},
    "alerts": [],
}
try:
    with open(STATE_FILE) as f:
        old = json.load(f)
    for k in ("flop", "racknerd", "alerts", "flop_agent", "bounty_radar", "testnet_radar", "radar_errors"):
        if k in old:
            state[k] = old[k]
except Exception:
    pass
# Normalisasi flop_agent: state.json lama mungkin belum punya semua key.
_fa = state.get("flop_agent", {})
state["flop_agent"] = {
    "status": _fa.get("status", "DORMANT"),
    "testnet_open": _fa.get("testnet_open", False),
    "faucet_url": _fa.get("faucet_url", ""),
    "inference_endpoint": _fa.get("inference_endpoint", ""),
    "last_check": _fa.get("last_check"),
}


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
                                         headers={**UA, "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"WEBHOOK OK {r.status}", flush=True)
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "replace")
            log(f"WEBHOOK FAIL: HTTPError {e.code} {body}", flush=True)
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


def sign_request(payload):
    """TODO: tanda tangan request inference dengan DID agen (riset SDK FLOP).

    JANGAN implementasikan sebelum riset SDK selesai. Fungsi ini TIDAK
    dipanggil di mana pun dalam scaffold ini, dan TIDAK PERNAH membaca
    kunci/seed dari disk (mis. ~/workspace/flop/identity.seed).
    """
    raise NotImplementedError("sign_request: TODO — menunggu hasil riset SDK FLOP")


def set_agent_status(new_status, note=""):
    """Pindah status flop_agent; alert via alert() hanya saat berubah."""
    with _lock:
        old = state["flop_agent"]["status"]
        if old == new_status:
            return False
        state["flop_agent"]["status"] = new_status
    save_state()
    alert("flop_agent", f"FLOP AGENT: {old} -> {new_status} {note}".strip())
    return True


def watch_flop_agent():
    """State machine agen FLOP: DORMANT -> READY -> WAITING_FAUCET ->
    ACTIVE -> PAUSED. Selama testnet belum buka, thread ini HANYA membaca
    penanda buka yang sudah dilacak watch_flop (state['flop']) + log;
    tidak ada aksi lain, tidak ada request ke faucet/inference."""
    errors = 0
    while True:
        try:
            with _lock:
                flop = dict(state["flop"])
                agent = dict(state["flop_agent"])
            markers = flop.get("markers") or []
            draft = flop.get("draft")
            # Marker "live" eksplisit menang atas kata "Draft" nyasar
            # (mis. di deskripsi link-preview halaman testnet): testnet
            # sudah buka begitu marker live muncul, walau draft=True.
            testnet_open = bool(markers) and (draft is False or "live" in markers)
            status = agent.get("status", "DORMANT")
            with _lock:
                state["flop_agent"].update({
                    "testnet_open": testnet_open,
                    "faucet_url": FAUCET_URL,
                    "inference_endpoint": INFERENCE_URL,
                    "last_check": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                })
            errors = 0
            if status == "PAUSED":
                # Pulih ke DORMANT begitu pengecekan jalan normal lagi.
                set_agent_status("DORMANT", "(pulih dari PAUSED)")
                status = "DORMANT"
            if status == "DORMANT" and testnet_open:
                set_agent_status("READY", "(testnet terdeteksi buka)")
                status = "READY"
            elif status == "READY" and not testnet_open:
                set_agent_status("DORMANT", "(penanda buka hilang lagi)")
                status = "DORMANT"
            if status == "READY":
                if FAUCET_URL and INFERENCE_URL:
                    # TODO: lanjut WAITING_FAUCET begitu riset SDK selesai.
                    set_agent_status("WAITING_FAUCET", "(faucet + inference terkonfigurasi)")
                    status = "WAITING_FAUCET"
                else:
                    set_watcher("flop_agent", "READY — menunggu FAUCET_URL/INFERENCE_URL (TODO riset SDK)")
            elif status in ("WAITING_FAUCET", "ACTIVE"):
                # TODO: klaim faucet -> beli inference (riset SDK).
                set_watcher("flop_agent", f"{status} — TODO: implementasi menunggu riset SDK")
            else:
                set_watcher("flop_agent", f"DORMANT — testnet belum buka (markers={markers or 'none'}, draft={draft})")
            wait = 60
        except Exception as e:
            errors += 1
            set_watcher("flop_agent", f"ERR {type(e).__name__}: {e} (error beruntun: {errors})")
            if errors >= 5 and state["flop_agent"]["status"] != "PAUSED":
                set_agent_status("PAUSED", f"(5 error beruntun: {type(e).__name__})")
            wait = 60
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
        self._quote = 0
        self._buf = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if tag == "a" and "Username" in cls and self._quote == 0:
            # A quoted member's name must never overwrite the comment author.
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
        if self._in_msg and tag == "blockquote":
            # Quoted parent post: NOT the author's own words — a member's
            # "giveaway" inside a quote must never trigger a round alert.
            self._quote += 1

    def handle_endtag(self, tag):
        if tag == "blockquote" and self._quote > 0:
            self._quote -= 1
        if tag == "a" and self._in_user:
            self._in_user = False
        if tag == "div" and self._in_msg:
            self._depth -= 1
            if self._depth <= 0:
                self._in_msg = False
                self._quote = 0
                text = " ".join("".join(self._buf).split())
                if self._author and text:
                    self.comments.append((self._author, text))
                self._buf = []

    def handle_data(self, data):
        if self._in_user:
            self._author = data.strip()
        if self._in_msg and self._quote == 0:
            self._buf.append(data)


def let_comments(url):
    status, body = fetch(url)
    p = LETParser()
    p.feed(body.decode("utf-8", "ignore"))
    return status, p.comments


def watch_racknerd(cfg):
    # A real round announcement, in dustinc's OWN words (LETParser strips
    # <blockquote> quotes, so a member's "giveaway" inside a quote can't
    # trigger): an entry rule "first/next N" + (eligible|screenshot|photo|
    # "shall receive"). Bandwidth confirmations ("bandwidth"/"doubled"/
    # "order #") never match. "giveaway" alone is NOT a trigger — it fired
    # on quoted member posts (p404 azmi_my, p414 zjuer, 25 Nov 2026) and on
    # teasers/winners posts. VM cron stays the authoritative backstop for
    # any round phrased differently.
    round_num = re.compile(r"\b(?:first|next)\s+\d+\b")
    round_word = ("eligible", "screenshot", "photo", "shall receive")
    bw_word = ("bandwidth", "doubled", "order #", "invoice")
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
                        is_round = (bool(round_num.search(low))
                                    and any(w in low for w in round_word)
                                    and not any(w in low for w in bw_word))
                        if not baseline and author.lower() == "dustinc" and is_round:
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


# --- Radar bounty + testnet (v5.4.0) ----------------------------------------
# Dua poller ringan (interval 60 menit), pola sama seperti watcher lain:
# seen-set di state.json, alert HANYA untuk item BARU, baseline di siklus
# pertama (tidak ada alert saat pertama kali jalan). Baseline dihitung
# sekali per siklus, sebelum sumber pertama, supaya tidak ada sumber yang
# alert di siklus pertama hanya karena sumber sebelumnya sudah menyimpan.
#
# watch_bounty_radar — program bounty BARU yang submission-nya gratis:
#   * Sherlock: API JSON publik https://audits.sherlock.xyz/api/contests
#     (?per_page=100, paginasi via next_page; total ~301, 4 halaman).
#     Item: id/title/status/type_label/prize_pool/token/starts_at/ends_at.
#     Link kontes: https://audits.sherlock.xyz/contests/<id> (terverifikasi
#     HTTP 200). Urutan API tidak jelas, jadi SEMUA halaman dipindai tiap
#     siklus dan seen-set yang memutuskan "baru".
#     PERINGATAN (riset 24 Nov 2026): API daftar HANYA memuat kontes FINISHED
#     (301) — /api/contests/stats melaporkan RUNNING:1 yang tidak pernah
#     muncul di daftar (filter ?status=RUNNING mengembalikan 0 item).
#     Karena itu thread juga memantau /api/contests/stats sebagai tripwire:
#     bila hitungan per status berubah vs siklus sebelumnya, alert dikirim
#     (tanpa detail — detailnya tidak tersedia dari API) supaya Jo mengecek
#     situsnya langsung.
#   * Cantina: halaman /opportunities/competitions dan /opportunities/bounties
#     meng-embed array JSON `items` (RSC payload, string JSON di-escape).
#     Item: id/name/url/status/totalRewardPot/currencyCode/timeframe +
#     entryFee (kompetisi) / submissionFee (bounty). Kompetisi: semua item
#     saat ini (2) ada di halaman; bounty: halaman 1 saja (10 dari 52,
#     urut pot terbesar) — ?page= diabaikan server (terverifikasi).
#   * Code4rena: halaman /audits (tile HTML) — slug/status/prize.
#     Alert hanya bila status BUKAN Completed (Live/Upcoming/Judging/
#     In progress). Saat riset (24 Nov 2026) halaman hanya memuat 50 audit
#     Completed — bila audit live muncul di halaman ini, radar menangkapnya;
#     bila C4 menaruh audit live di rute lain, itu keterbatasan yang dicatat.
#   * Immunefi: TIDAK BISA — halaman /bug-bounty/ hanya me-render 10 program
#     featured; daftar penuh (152 program) dimuat client-side via JS, tidak
#     ada API publik yang ditemukan dan tidak ada sitemap. Dilewati.
#   Fee: TIDAK ADA alert yang mengklaim "gratis" tanpa bukti. Bila data
#   tidak memuat info fee -> "cek fee dulu". Bila data Cantina memuat
#   entryFee/submissionFee -> nilainya dicantumkan apa adanya.
# watch_testnet_radar — testnet BERINSENTIF baru:
#   * airdrops.io/latest (HTML, kartu artikel) — filter: teks kartu
#     mengandung "testnet". Kartu tidak membawa tanggal, jadi "baru" =
#     slug yang belum pernah terlihat di daftar ini.
#     PERINGATAN (24 Nov 2026): airdrops.io mengembalikan 403 dari
#     egress cloud (box Nodaroo + box jo-companion), padahal 200 dari VM
#     — blokir IP datacenter oleh Cloudflare; varian header (Referer,
#     Accept-Language, Sec-Fetch) tidak membantu. Kaki ini best-effort.
#   * 99airdrops.com/testnet-airdrops (HTML, daftar khusus testnet, 34 item).
#   * airdropalert.com/testnet-airdrops (v5.4.1, kaki cadangan — 200 dari
#     VM DAN box jo-companion; daftar khusus testnet, 12 item/halaman,
#     halaman 1-2 dipindai).
# Sumber yang DICOBA dan DITOLAK: cryptorank.io/airdrops (403 dari VM dan
# box), coingecko.com/en/airdrops (403), airdropking.io (redirect ke
# marketdigest.io 522), https://airdrops.io/testnets/ (404),
# icodrops.com/airdrops/ (404).
RADAR_INTERVAL = 3600  # 60 menit


def _extract_cantina_items(html):
    """Ambil array `items` dari JSON yang di-embed di halaman Cantina
    (RSC payload: string JSON yang di-escape). Kembalikan list dict."""
    marker = '\\"items\\":['
    i = html.find(marker)
    if i < 0:
        return []
    j = html.find("[", i)
    depth = 0
    k = j
    while k < len(html):
        c = html[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        k += 1
    if depth != 0:
        return []
    raw = html[j:k + 1]
    try:
        return json.loads(raw.replace('\\"', '"'))
    except Exception:
        return []


def _sherlock_contests():
    """Semua kontes Sherlock via API JSON publik (100/page, paginasi)."""
    out = []
    page = 1
    while page <= 10:
        status, body = fetch(f"https://audits.sherlock.xyz/api/contests?per_page=100&page={page}")
        if status != 200:
            raise RuntimeError(f"Sherlock API HTTP {status}")
        d = json.loads(body.decode("utf-8", "ignore"))
        items = d.get("items") or []
        out.extend(items)
        if not d.get("has_next"):
            break
        page = d.get("next_page") or page + 1
    return out


def _c4_audits():
    """Audit Code4rena dari halaman /audits (tile HTML)."""
    status, body = fetch("https://code4rena.com/audits")
    html = body.decode("utf-8", "ignore")
    out = []
    for href, inner in re.findall(r'<a class="audit-tile__link" href="([^"]+)">(.*?)</a>', html, re.S):
        txt = re.sub(r"<[^>]+>", "|", inner)
        m = re.search(r"\|\s*(Live|Upcoming|Completed|Judging|In progress|Cancelled)\b", txt)
        st = m.group(1) if m else "?"
        prize = re.search(r"\$[\d,]+(?:\.\d+)?(?:\s*in\s*[A-Z]+)?", txt)
        slug = href.rsplit("/", 1)[-1]
        out.append({
            "slug": slug,
            "url": "https://code4rena.com" + href,
            "status": st,
            "prize": prize.group(0) if prize else "",
        })
    return out


def _airdrops_io_latest():
    """Kartu-kartu terbaru dari https://airdrops.io/latest/ (HTML)."""
    status, body = fetch("https://airdrops.io/latest/")
    page = body.decode("utf-8", "ignore")
    out = []
    for art in re.findall(r"<article[^>]*>(.*?)</article>", page, re.S):
        links = re.findall(r'href="(https://airdrops\.io/([a-z0-9-]+)/)"', art)
        if not links:
            continue
        url, slug = links[0]
        name = re.search(r"<h[23][^>]*>(.*?)</h[23]>", art, re.S)
        name = re.sub(r"<[^>]+>", "", name.group(1)).strip() if name else slug
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", art)).strip()
        out.append({"slug": slug, "name": html.unescape(name), "url": url, "text": text})
    return out


def _airdrops99_testnets():
    """Daftar testnet dari https://99airdrops.com/testnet-airdrops/ (HTML)."""
    status, body = fetch("https://99airdrops.com/testnet-airdrops/")
    html = body.decode("utf-8", "ignore")
    out = []
    for slug in re.findall(r'href="/airdrops/([a-z0-9-]+)/"', html):
        if any(o["slug"] == slug for o in out):
            continue
        out.append({"slug": slug, "name": slug.replace("-", " ").title(),
                    "url": f"https://99airdrops.com/airdrops/{slug}/"})
    return out


def _airdropalert_testnets():
    """Daftar testnet dari https://airdropalert.com/testnet-airdrops/
    (HTML server-rendered, halaman 1-2; 12 item/halaman). Halaman 1 wajib
    berhasil; halaman 2 best-effort (gagal -> pakai hasil halaman 1)."""
    out = []
    for pno in (1, 2):
        url = ("https://airdropalert.com/testnet-airdrops/" if pno == 1
               else f"https://airdropalert.com/testnet-airdrops/page/{pno}/")
        try:
            status, body = fetch(url)
            page = body.decode("utf-8", "ignore")
        except Exception:
            if pno == 1:
                raise
            break
        for slug in re.findall(r'href="https://airdropalert\.com/airdrops/([a-z0-9-]+)/"', page):
            if any(o["slug"] == slug for o in out):
                continue
            out.append({"slug": slug, "name": slug.replace("-", " ").title(),
                        "url": f"https://airdropalert.com/airdrops/{slug}/"})
    return out


def _radar_seen(key):
    return set(state[key]["seen"])


def _radar_save(key, seen):
    with _lock:
        state[key]["seen"] = sorted(seen)[-500:]
    save_state()


def _radar_source(radar_key, source, ok, short, detail=""):
    """Lacak status satu sumber radar; alert Discord SEKALI saat transisi
    OK->ERR dan ERR->OK (status tersimpan di state['radar_errors']).
    Tidak spam bila error menetap: hanya transisi yang alert.
    Kembalikan note singkat untuk set_watcher. Dipanggil sekali per
    siklus per sumber, di jalur sukses maupun gagal."""
    key = f"{radar_key}:{source}"
    with _lock:
        errs = state.setdefault("radar_errors", {})
        prev = errs.get(key, "ok")
        errs[key] = "ok" if ok else f"ERR {short}"
    save_state()
    if ok and prev != "ok":
        alert(radar_key, f"RADAR PULIH [{source}] {radar_key}: {detail or 'ok lagi'} (sebelumnya {prev})")
    elif not ok and prev == "ok":
        hint = ""
        if source == "airdrops.io" and "403" in (detail or ""):
            hint = " (kemungkinan blokir IP datacenter oleh Cloudflare; kaki cadangan airdropalert tetap jalan)"
        alert(radar_key, f"RADAR ERROR [{source}] {radar_key}: {detail or short}{hint}")
    return f"{source} {'ok' if ok else 'ERR ' + short}"


def watch_bounty_radar():
    while True:
        try:
            alerts = []
            notes = []
            baseline = not _radar_seen("bounty_radar")
            # --- Sherlock (API JSON) ---
            try:
                seen = _radar_seen("bounty_radar")
                for it in _sherlock_contests():
                    key = f"sherlock:{it.get('id')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if not baseline:
                        pool = it.get("prize_pool")
                        pool_s = f"{pool:,} {it.get('token', '')}".strip() if isinstance(pool, (int, float)) else "?"
                        start = time.strftime("%Y-%m-%d", time.gmtime(it["starts_at"])) if it.get("starts_at") else "?"
                        alerts.append(f"BOUNTY BARU [Sherlock] {it.get('title', '?')} — {it.get('type_label', '')}, pool {pool_s}, status {it.get('status', '?')}, mulai {start} — https://audits.sherlock.xyz/contests/{it.get('id')} — fee: cek fee dulu (tidak ada info fee di data)")
                _radar_save("bounty_radar", seen)
                _radar_source("bounty_radar", "sherlock", True, "ok")
                # Tripwire: /api/contests/stats — API daftar hanya memuat
                # FINISHED, jadi perubahan hitungan per status adalah satu-
                # satunya sinyal kontes live baru.
                try:
                    _, sbody = fetch("https://audits.sherlock.xyz/api/contests/stats")
                    stats = {row["status"]: row["count"] for row in json.loads(sbody.decode("utf-8", "ignore"))}
                    with _lock:
                        prev = state["bounty_radar"].get("sherlock_stats") or {}
                        state["bounty_radar"]["sherlock_stats"] = stats
                    save_state()
                    if prev and stats != prev:
                        diffs = [f"{k} {prev.get(k, 0)}→{v}" for k, v in stats.items() if prev.get(k, 0) != v]
                        diffs += [f"{k} {prev.get(k, 0)}→0 (hilang)" for k in prev if k not in stats]
                        alerts.append(f"SHERLOCK STATS BERUBAH: {', '.join(diffs)} — ada kontes baru/berubah status (API daftar hanya memuat kontes FINISHED, detail tidak tersedia) — cek https://audits.sherlock.xyz/contests")
                    notes.append(f"sherlock ok ({len(seen)} seen, stats={stats})")
                    _radar_source("bounty_radar", "sherlock-stats", True, "ok")
                except Exception as e:
                    notes.append(_radar_source("bounty_radar", "sherlock-stats", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                    log(f"BOUNTY_RADAR sherlock stats ERR: {type(e).__name__}: {e}", flush=True)
            except Exception as e:
                notes.append(_radar_source("bounty_radar", "sherlock", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                log(f"BOUNTY_RADAR sherlock ERR: {type(e).__name__}: {e}", flush=True)
            # --- Cantina (kompetisi + bounty, JSON ter-embed) ---
            try:
                cantina_cf = None
                for label, url, fee_field, pages in (
                    ("Cantina-kompetisi", "https://cantina.xyz/opportunities/competitions", "entryFee", 1),
                    # Bounty: ?page= diabaikan server (terverifikasi 24 Nov 2026:
                    # halaman 2 mengembalikan 10 item yang sama) — halaman 1
                    # saja (10 dari 52, urut pot terbesar).
                    ("Cantina-bounty", "https://cantina.xyz/opportunities/bounties", "submissionFee", 1),
                ):
                    for pno in range(1, pages + 1):
                        try:
                            _, body = fetch(url if pno == 1 else f"{url}?page={pno}")
                            items = _extract_cantina_items(body.decode("utf-8", "ignore"))
                        except urllib.error.HTTPError as e:
                            if e.code in (403, 429):
                                cantina_cf = f"{label} CLOUDFLARE {e.code}"
                                break
                            raise
                        seen = _radar_seen("bounty_radar")
                        for it in items:
                            key = f"cantina:{it.get('id')}"
                            if key in seen:
                                continue
                            seen.add(key)
                            if baseline:
                                continue
                            if str(it.get("status", "")).lower() not in ("live", "upcoming", "open"):
                                continue
                            pot = it.get("totalRewardPot", "?")
                            cur = it.get("currencyCode", "")
                            fee = it.get(fee_field)
                            if fee not in (None, "", "0.00", "0"):
                                fee_s = f"cek fee dulu (data Cantina: {fee_field} ${fee} {cur})"
                            else:
                                fee_s = "cek fee dulu (tidak ada info fee di data)"
                            tf = it.get("timeframe") or {}
                            end = (tf.get("end") or "?")[:10]
                            alerts.append(f"BOUNTY BARU [{label}] {it.get('name', '?')} — {it.get('company', {}).get('name', '')}, pool {pot} {cur}, status {it.get('status', '?')}, {str(tf.get('start', '?'))[:10]} s.d. {end} — {it.get('url', '')} — fee: {fee_s}")
                        _radar_save("bounty_radar", seen)
                if cantina_cf:
                    notes.append(_radar_source("bounty_radar", "cantina", False, cantina_cf, f"Cantina {cantina_cf}"))
                else:
                    notes.append("cantina ok")
                    _radar_source("bounty_radar", "cantina", True, "ok")
            except Exception as e:
                notes.append(_radar_source("bounty_radar", "cantina", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                log(f"BOUNTY_RADAR cantina ERR: {type(e).__name__}: {e}", flush=True)
            # --- Code4rena (tile HTML) ---
            try:
                seen = _radar_seen("bounty_radar")
                for it in _c4_audits():
                    key = f"c4:{it['slug']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if baseline:
                        continue
                    if it["status"] not in ("Live", "Upcoming", "Judging", "In progress"):
                        continue
                    parts = it["slug"].split("-", 2)
                    name = parts[-1].replace("-", " ").title() if len(parts) == 3 and parts[0].isdigit() else it["slug"].replace("-", " ").title()
                    alerts.append(f"BOUNTY BARU [Code4rena] {name} — status {it['status']}, hadiah {it['prize'] or '?'} — {it['url']} — fee: cek fee dulu (tidak ada info fee di data)")
                _radar_save("bounty_radar", seen)
                notes.append("c4 ok")
                _radar_source("bounty_radar", "c4", True, "ok")
            except Exception as e:
                notes.append(_radar_source("bounty_radar", "c4", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                log(f"BOUNTY_RADAR c4 ERR: {type(e).__name__}: {e}", flush=True)
            for msg in alerts:
                alert("bounty_radar", msg)
            set_watcher("bounty_radar", "; ".join(notes) + (f"; {len(alerts)} alert baru" if alerts else ""))
        except Exception as e:
            set_watcher("bounty_radar", f"ERR {type(e).__name__}: {e}")
        time.sleep(RADAR_INTERVAL)


def watch_testnet_radar():
    while True:
        try:
            alerts = []
            notes = []
            baseline = not _radar_seen("testnet_radar")
            # --- airdrops.io/latest (kartu testnet) ---
            try:
                seen = _radar_seen("testnet_radar")
                for it in _airdrops_io_latest():
                    if "testnet" not in it["text"].lower():
                        continue
                    key = f"airdropsio:{it['slug']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if not baseline:
                        alerts.append(f"TESTNET BARU [airdrops.io] {it['name']} — {it['url']} — insentif: cek halaman proyek (kartu menyebut testnet)")
                _radar_save("testnet_radar", seen)
                notes.append("airdrops.io ok")
                _radar_source("testnet_radar", "airdrops.io", True, "ok")
            except Exception as e:
                notes.append(_radar_source("testnet_radar", "airdrops.io", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                log(f"TESTNET_RADAR airdrops.io ERR: {type(e).__name__}: {e}", flush=True)
            # --- 99airdrops /testnet-airdrops ---
            try:
                seen = _radar_seen("testnet_radar")
                for it in _airdrops99_testnets():
                    key = f"99airdrops:{it['slug']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if not baseline:
                        alerts.append(f"TESTNET BARU [99airdrops] {it['name']} — {it['url']} — insentif: cek halaman proyek (daftar testnet 99airdrops)")
                _radar_save("testnet_radar", seen)
                notes.append("99airdrops ok")
                _radar_source("testnet_radar", "99airdrops", True, "ok")
            except Exception as e:
                notes.append(_radar_source("testnet_radar", "99airdrops", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                log(f"TESTNET_RADAR 99airdrops ERR: {type(e).__name__}: {e}", flush=True)
            # --- airdropalert.com/testnet-airdrops (kaki cadangan v5.4.1) ---
            try:
                seen = _radar_seen("testnet_radar")
                # Baseline per-sumber: kaki baru pertama kali jalan tidak
                # alert untuk item lama (seen-set lama belum punya kunci
                # "airdropalert:" sama sekali) — hanya simpan.
                aa_baseline = not any(k.startswith("airdropalert:") for k in seen)
                for it in _airdropalert_testnets():
                    key = f"airdropalert:{it['slug']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if not baseline and not aa_baseline:
                        alerts.append(f"TESTNET BARU [airdropalert] {it['name']} — {it['url']} — insentif: cek halaman proyek (daftar testnet airdropalert)")
                _radar_save("testnet_radar", seen)
                notes.append("airdropalert ok")
                _radar_source("testnet_radar", "airdropalert", True, "ok")
            except Exception as e:
                notes.append(_radar_source("testnet_radar", "airdropalert", False, type(e).__name__, f"{type(e).__name__}: {e}"))
                log(f"TESTNET_RADAR airdropalert ERR: {type(e).__name__}: {e}", flush=True)
            for msg in alerts:
                alert("testnet_radar", msg)
            set_watcher("testnet_radar", "; ".join(notes) + (f"; {len(alerts)} alert baru" if alerts else ""))
        except Exception as e:
            set_watcher("testnet_radar", f"ERR {type(e).__name__}: {e}")
        time.sleep(RADAR_INTERVAL)


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


def load_private_config():
    """Clone the private config repo (if USERNAME+ACCESS_TOKEN are set) and
    read DISCORD_WEBHOOK from config/config.json. The token is NEVER printed
    or stored: the clone URL lives only in memory, stderr is discarded, and
    the remote URL is stripped right after cloning. Any failure leaves the
    watchers running without a webhook. On success, posts one boot-test
    message so the wiring is proven end-to-end immediately."""
    user = os.environ.get("USERNAME")
    token = os.environ.get("ACCESS_TOKEN")
    if not user or not token:
        print("CONFIG: no USERNAME/ACCESS_TOKEN — webhook stays off", flush=True)
        return
    repo = "tiVan0/jo-box-secrets"
    try:
        shutil.rmtree("config", ignore_errors=True)
        url = f"https://{user}:{token}@github.com/{repo}.git"
        r = subprocess.run(["git", "clone", "--depth", "1", url, "config"],
                           timeout=60, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"CONFIG: clone FAIL rc={r.returncode} — webhook stays off", flush=True)
            return
        subprocess.run(["git", "-C", "config", "remote", "set-url", "origin",
                        f"https://github.com/{repo}.git"],
                       timeout=30, capture_output=True, text=True)
        with open("config/config.json") as f:
            cfg = json.load(f)
        hook = cfg.get("DISCORD_WEBHOOK", "").strip()
        if not hook.startswith("https://discord.com/api/webhooks/"):
            print("CONFIG: config.json has no valid DISCORD_WEBHOOK — webhook stays off", flush=True)
            return
        os.environ["DISCORD_WEBHOOK"] = hook
        print("CONFIG OK: webhook loaded (private repo)", flush=True)
        try:
            req = urllib.request.Request(
                hook,
                data=json.dumps({"content": f"Jo-box {VERSION} online — watchers + radar aktif (FLOP + RackNerd + Delphi + bounty_radar + testnet_radar)"}).encode(),
                headers={**UA, "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"CONFIG: webhook boot test OK {resp.status}", flush=True)
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "replace")
            print(f"CONFIG: webhook boot test FAIL HTTPError {e.code} {body}", flush=True)
        except Exception as e:
            print(f"CONFIG: webhook boot test FAIL {type(e).__name__}", flush=True)
    except Exception as e:
        print(f"CONFIG FAIL: {type(e).__name__} — webhook stays off", flush=True)


print(f"=== Jo-box test {VERSION} — watchers + radar ===", flush=True)
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

load_private_config()

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
threading.Thread(target=watch_flop_agent, daemon=True).start()
threading.Thread(target=watch_bounty_radar, daemon=True).start()
threading.Thread(target=watch_testnet_radar, daemon=True).start()
log("WATCHERS started: delphi + flop + racknerd + flop_agent + bounty_radar + testnet_radar", flush=True)
save_state()

n = 0
while True:
    n += 1
    print(f"heartbeat {n} - Jo-box hidup {n * 30} detik [{VERSION} watchers + radar]", flush=True)
    time.sleep(30)

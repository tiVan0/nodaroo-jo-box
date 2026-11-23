"""Jo-box app v4: one-time git bootstrap + env/egress test + heartbeat.

The panel console sends input to the app's stdin, not a shell, so git
commands cannot be typed there. This app links its own folder to the
GitHub repo on first run (idempotent: skipped once .git exists).
After this, the startup's `git pull` (AUTO_UPDATE=1) keeps it updated.
"""
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

REPO = "https://github.com/tiVan0/nodaroo-jo-box"


def run(cmd):
    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip().replace("\n", " | ")[:300]
        print(f"BOOT {'OK' if r.returncode == 0 else 'FAIL'} {' '.join(cmd)} -> {out}", flush=True)
        return r.returncode == 0
    except Exception as e:
        print(f"BOOT FAIL {' '.join(cmd)} -> {type(e).__name__}: {e}", flush=True)
        return False


print("=== Jo-box test v4 ===", flush=True)
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

n = 0
while True:
    n += 1
    print(f"heartbeat {n} - Jo-box hidup {n * 30} detik [v4 via git pull]", flush=True)
    time.sleep(30)

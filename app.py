import os
import platform
import socket
import sys
import time
import urllib.request

print("=== Jo-box test v2 ===", flush=True)
print("python:", sys.version.split()[0], flush=True)
print("platform:", platform.platform(), flush=True)
print("hostname:", socket.gethostname(), flush=True)
print("cpu count:", os.cpu_count(), flush=True)
print("cwd:", os.getcwd(), flush=True)
print("files:", sorted(os.listdir(".")))

for url in ("https://ifconfig.me/ip", "https://api.github.com/"):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            print(f"EGRESS OK {url} -> {r.status} {r.read(120)!r}", flush=True)
    except Exception as e:
        print(f"EGRESS FAIL {url} -> {type(e).__name__}: {e}", flush=True)

n = 0
while True:
    n += 1
    print(f"heartbeat {n} - Jo-box hidup {n * 30} detik", flush=True)
    time.sleep(30)

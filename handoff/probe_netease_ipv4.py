import io
import json
import os
import socket
import sys
from urllib.request import Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth as na

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = na.NeteaseAuth(path).load()
cookie = "; ".join(f"{name}={value}" for name, value in session._cookies)
out = io.StringIO()

for host in ("music.163.com", "m704.music.126.net", "m804.music.126.net"):
    try:
        info = socket.getaddrinfo(host, 443)
        families = sorted({("IPv6" if item[0] == socket.AF_INET6 else "IPv4") for item in info})
        out.write(f"{host}: {families}\n")
    except Exception as error:
        out.write(f"{host}: {type(error).__name__} {error}\n")

_orig = socket.getaddrinfo


def ipv4_only(host, port, *args, **kwargs):
    return [item for item in _orig(host, port, *args, **kwargs) if item[0] == socket.AF_INET]


def get_url(use_ipv4):
    body = na._weapi_body({"id": 34338105, "ids": "[34338105]", "br": 999000})
    request = Request("https://music.163.com/weapi/song/enhance/player/url", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    opener = build_opener(na._NoRedirect())
    with opener.open(request, timeout=15) as response:
        return json.loads(response.read().decode())["data"][0]["url"]


def fetch(url, use_ipv4):
    target = url.replace("http://", "https://", 1)
    with build_opener().open(Request(target, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as response:
        return f"status={response.status} len={response.headers.get('Content-Length')!r}"


socket.getaddrinfo = ipv4_only
try:
    url = get_url(True)
    out.write(f"[ipv4 api] url={'yes' if url else 'none'}\n")
    out.write(f"[ipv4 api -> ipv4 cdn] {fetch(url, True)}\n")
except Exception as error:
    out.write(f"[ipv4 path] {type(error).__name__}: {error}\n")
finally:
    socket.getaddrinfo = _orig

with open(r"D:\AMDL\handoff\netease-ipv4.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

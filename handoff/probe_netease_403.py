import json
import os
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth as na

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = na.NeteaseAuth(path).load()
cookie = "; ".join(f"{name}={value}" for name, value in session._cookies)
body = na._weapi_body({"id": 34338105, "ids": "[34338105]", "br": 999000})
request = Request("https://music.163.com/weapi/song/enhance/player/url", data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
    item = json.loads(response.read().decode())["data"][0]
url = item["url"]
host = url.split("/")[2]
lines = [f"host={host}", f"dns={sorted({info[4][0] for info in socket.getaddrinfo(host, 443)})}"]
for label, target in (("https", url.replace("http://", "https://", 1)), ("http", url)):
    started = time.monotonic()
    try:
        with build_opener().open(Request(target, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as response:
            lines.append(f"[{label}] status={response.status} in {time.monotonic()-started:.1f}s")
    except HTTPError as error:
        raw = error.read()[:1500]
        lines.append(f"[{label}] status={error.code} in {time.monotonic()-started:.1f}s "
                     f"headers={{'Server': {error.headers.get('Server')!r}, 'X-Cache': {error.headers.get('X-Cache')!r}}}")
        lines.append(f"[{label}] body={raw!r}")
    except Exception as error:
        lines.append(f"[{label}] {type(error).__name__} in {time.monotonic()-started:.1f}s: {error}")
with open(r"D:\AMDL\handoff\netease-403.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

import json
import os
import sys
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
    url = json.loads(response.read().decode())["data"][0]["url"]
url = url.replace("http://", "https://", 1)

FULL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
variants = (
    ("full-ua", {"User-Agent": FULL_UA, "Accept-Encoding": "identity"}),
    ("full-ua+referer", {"User-Agent": FULL_UA, "Referer": "https://music.163.com/",
                         "Accept-Encoding": "identity"}),
    ("full-ua+referer+cookie", {"User-Agent": FULL_UA, "Referer": "https://music.163.com/",
                                "Cookie": cookie, "Accept-Encoding": "identity"}),
    ("range", {"User-Agent": FULL_UA, "Referer": "https://music.163.com/",
               "Range": "bytes=0-15", "Accept-Encoding": "identity"}),
)
lines = []
for label, headers in variants:
    try:
        with build_opener().open(Request(url, headers=headers), timeout=25) as response:
            lines.append(f"[{label}] status={response.status} type={response.headers.get('Content-Type')!r} "
                         f"len={response.headers.get('Content-Length')!r} prefix={response.read(8)!r}")
    except Exception as error:
        lines.append(f"[{label}] {type(error).__name__}: {error}")
with open(r"D:\AMDL\handoff\netease-ua.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

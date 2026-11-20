import os
import sys
from urllib.request import Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth
from netease_auth import netease_api_request

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = netease_auth.NeteaseAuth(path).load()
song_id = 34338105
reply = netease_api_request(session, "/api/song/enhance/player/url",
                            {"id": song_id, "ids": f"[{song_id}]", "br": 999000})
url = reply["data"][0]["url"]
cookie = "; ".join(f"{name}={value}" for name, value in session._cookies)

variants = (
    ("plain", {"User-Agent": "Mozilla/5.0"}),
    ("referer", {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"}),
    ("cookie", {"User-Agent": "Mozilla/5.0", "Cookie": cookie}),
    ("both", {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/", "Cookie": cookie}),
    ("both+origin", {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/",
                     "Origin": "https://music.163.com", "Cookie": cookie}),
)
lines = []
for label, headers in variants:
    headers = {**headers, "Accept-Encoding": "identity"}
    try:
        request = Request(url, headers=headers)
        with build_opener().open(request, timeout=30) as response:
            lines.append(f"[{label}] status={response.status} type={response.headers.get('Content-Type')!r} "
                         f"len={response.headers.get('Content-Length')!r} prefix={response.read(8)!r}")
    except Exception as error:
        lines.append(f"[{label}] ERROR {type(error).__name__}: {error}")

with open(r"D:\AMDL\handoff\netease-cdn-headers2.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

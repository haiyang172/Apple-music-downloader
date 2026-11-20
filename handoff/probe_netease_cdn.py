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
item = reply["data"][0]
url = item["url"]
lines = [f"url: {url[:120]}...", f"type={item.get('type')} level={item.get('level')} size={item.get('size')}"]

https = url.replace("http://", "https://", 1)
lines.append(f"https url: {https[:120]}...")
for label, target in (("https", https), ("http", url)):
    try:
        request = Request(target, headers={"User-Agent": "Mozilla/5.0", "Accept": "audio/flac,audio/x-flac,application/octet-stream",
                                           "Accept-Encoding": "identity"})
        with build_opener().open(request, timeout=30) as response:
            lines.append(f"[{label}] status={response.status} content-type={response.headers.get('Content-Type')!r} "
                         f"len={response.headers.get('Content-Length')!r} enc={response.headers.get('Content-Encoding')!r} "
                         f"range={response.headers.get('Content-Range')!r} te={response.headers.get('Transfer-Encoding')!r}")
            prefix = response.read(16)
            lines.append(f"[{label}] prefix={prefix!r}")
            break
    except Exception as error:
        lines.append(f"[{label}] ERROR {type(error).__name__}: {error}")

with open(r"D:\AMDL\handoff\netease-cdn-headers.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

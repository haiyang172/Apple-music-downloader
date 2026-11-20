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
song_id = 34338105
lines = []
for label, with_realip in (("with-realIP", True), ("no-realIP", False)):
    params = {"id": song_id, "ids": f"[{song_id}]", "br": 999000}
    if with_realip:
        params["realIP"] = na._REAL_IP
    body = na._weapi_body(params)
    request = Request("https://music.163.com/weapi/song/enhance/player/url", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    try:
        with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as error:
        lines.append(f"[{label}] api ERROR {type(error).__name__}: {error}")
        continue
    items = payload.get("data") or []
    item = items[0] if items else {}
    url = item.get("url") or ""
    lines.append(f"[{label}] api code={payload.get('code')} item code={item.get('code')} type={item.get('type')} "
                 f"level={item.get('level')} url={'yes' if url else 'none'}")
    if url:
        try:
            fetch = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
            with build_opener().open(fetch, timeout=30) as response:
                lines.append(f"[{label}] cdn status={response.status} type={response.headers.get('Content-Type')!r} "
                             f"len={response.headers.get('Content-Length')!r} prefix={response.read(8)!r}")
        except Exception as error:
            lines.append(f"[{label}] cdn ERROR {type(error).__name__}: {error}")

with open(r"D:\AMDL\handoff\netease-realip.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

import io
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
out = io.StringIO()

params = {"ids": "[34338105]", "level": "exhigh", "encodeType": "flac", "realIP": na._REAL_IP}
hexed = na._eapi_body("/api/song/enhance/player/url/v1", params)
for label, body in (("no-prefix", hexed), ("params=", b"params=" + hexed)):
    request = Request("https://music.163.com/eapi/song/enhance/player/url/v1", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    try:
        with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(4000)
        out.write(f"[{label}] status=200 len={len(raw)} head={raw[:80]!r}\n")
        try:
            payload = json.loads(raw.decode("utf-8"))
            items = payload.get("data") or []
            item = items[0] if isinstance(items, list) and items else {}
            out.write(f"[{label}] code={payload.get('code')} item code={item.get('code')} type={item.get('type')} "
                      f"level={item.get('level')} br={item.get('br')} url={(item.get('url') or '')[:60]}\n")
        except Exception as error:
            out.write(f"[{label}] json error {type(error).__name__}: {error}\n")
    except Exception as error:
        out.write(f"[{label}] {type(error).__name__}: {error}\n")

with open(r"D:\AMDL\handoff\netease-eapi.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

import io
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth as na

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = na.NeteaseAuth(path).load()
cookie = "; ".join(f"{name}={value}" for name, value in session._cookies)
out = io.StringIO()

UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_WEB = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
HEADER = json.dumps({"os": "pc", "appver": "8.9.70", "osver": "", "deviceId": "amdlprobe",
                     "requestId": "amdlprobe0001"}, separators=(",", ":"))

variants = (
    ("no-header", {"ids": "[34338105]", "level": "exhigh", "encodeType": "flac", "realIP": na._REAL_IP}, UA_CHROME),
    ("with-header", {"ids": "[34338105]", "level": "exhigh", "encodeType": "flac", "realIP": na._REAL_IP,
                     "header": HEADER}, UA_CHROME),
    ("header+webua", {"ids": "[34338105]", "level": "exhigh", "encodeType": "flac", "realIP": na._REAL_IP,
                      "header": HEADER}, UA_WEB),
)
for label, params, ua in variants:
    body = b"params=" + na._eapi_body("/api/song/enhance/player/url/v1", params)
    request = Request("https://music.163.com/eapi/song/enhance/player/url/v1", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": ua,
                               "Referer": "https://music.163.com/", "Origin": "https://music.163.com"})
    try:
        with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        out.write(f"[{label}] api {type(error).__name__}: {error}\n")
        continue
    items = payload.get("data") or []
    item = items[0] if isinstance(items, list) and items else {}
    url = item.get("url") or ""
    out.write(f"[{label}] code={payload.get('code')} item={item.get('code')} host={(url.split('/')[2] if url else 'none')}\n")
    if not url:
        continue
    for fetch_label, headers in (("ua-match", {"User-Agent": ua, "Referer": "https://music.163.com/",
                                               "Cookie": cookie, "Accept-Encoding": "identity"}),):
        try:
            with build_opener().open(Request(url.replace("http://", "https://", 1), headers=headers),
                                     timeout=25) as response:
                out.write(f"   [{fetch_label}] status={response.status} len={response.headers.get('Content-Length')!r}\n")
        except HTTPError as error:
            out.write(f"   [{fetch_label}] HTTP {error.code}\n")
        except Exception as error:
            out.write(f"   [{fetch_label}] {type(error).__name__}: {error}\n")

with open(r"D:\AMDL\handoff\netease-header.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

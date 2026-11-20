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
lines = []


def api(url_path, params):
    body = na._weapi_body(params)
    request = Request("https://music.163.com" + url_path, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def fetch(url):
    for label, target in (("https", url.replace("http://", "https://", 1)), ("http", url)):
        try:
            with build_opener().open(Request(target, headers={"User-Agent": "Mozilla/5.0",
                                                              "Accept-Encoding": "identity"}), timeout=20) as response:
                return f"{label} status={response.status} type={response.headers.get('Content-Type')!r} " \
                       f"len={response.headers.get('Content-Length')!r} prefix={response.read(8)!r}"
        except Exception as error:
            last = f"{label} {type(error).__name__}: {error}"
    return last


tests = (
    ("player br=999000", "/weapi/song/enhance/player/url", {"id": 34338105, "ids": "[34338105]", "br": 999000}),
    ("player br=320000", "/weapi/song/enhance/player/url", {"id": 34338105, "ids": "[34338105]", "br": 320000}),
    ("player br=128000", "/weapi/song/enhance/player/url", {"id": 34338105, "ids": "[34338105]", "br": 128000}),
    ("download br=999000", "/weapi/song/enhance/download/url", {"id": 34338105, "ids": "[34338105]", "br": 999000}),
    ("download br=320000", "/weapi/song/enhance/download/url", {"id": 34338105, "ids": "[34338105]", "br": 320000}),
)
for label, url_path, params in tests:
    try:
        payload = api(url_path, params)
    except Exception as error:
        lines.append(f"[{label}] api ERROR {type(error).__name__}: {error}")
        continue
    items = payload.get("data")
    if isinstance(items, list):
        item = items[0] if items else {}
    elif isinstance(items, dict):
        item = items
    else:
        item = {}
    url = item.get("url") or ""
    lines.append(f"[{label}] api code={payload.get('code')} item code={item.get('code')} "
                 f"type={item.get('type')} level={item.get('level')} br={item.get('br')} size={item.get('size')} "
                 f"url={'yes' if url else 'none'}")
    if url:
        lines.append("   " + fetch(url))

with open(r"D:\AMDL\handoff\netease-qualities.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

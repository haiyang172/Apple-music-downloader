import io
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth as na

PROXY = "http://127.0.0.1:56590"
out = io.StringIO()
path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = na.NeteaseAuth(path).load()
cookie = "; ".join(f"{name}={value}" for name, value in session._cookies)


def open_with_proxy(url, timeout=25):
    opener = build_opener(ProxyHandler({"http": PROXY, "https": PROXY}))
    return opener.open(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout)


def api(song_id):
    body = na._weapi_body({"id": song_id, "ids": f"[{song_id}]", "br": 999000})
    request = Request("https://music.163.com/weapi/song/enhance/player/url", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


# 1) Song-by-song API result.
for song_id, title in ((34338105, "Bella Ciao"), (1895330088, "予你"), (441491828, "水星记"),
                       (1929114538, "我多想说再见啊")):
    try:
        payload = api(song_id)
    except Exception as error:
        out.write(f"[{title}] api ERROR {type(error).__name__}: {error}\n")
        continue
    items = payload.get("data") or []
    item = items[0] if isinstance(items, list) and items else {}
    out.write(f"[{title}] code={payload.get('code')} item code={item.get('code')} type={item.get('type')} "
              f"level={item.get('level')} br={item.get('br')} size={item.get('size')} "
              f"freeTrial={item.get('freeTrialInfo')} url={'yes' if item.get('url') else 'none'}\n")

# 2) Proxy reachability for the CDN.
try:
    url = api(34338105)["data"][0]["url"]
    with open_with_proxy(url) as response:
        out.write(f"[proxy-cdn] status={response.status} len={response.headers.get('Content-Length')!r} "
                  f"prefix={response.read(8)!r}\n")
except Exception as error:
    out.write(f"[proxy-cdn] {type(error).__name__}: {error}\n")

with open(r"D:\AMDL\handoff\netease-proxy.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

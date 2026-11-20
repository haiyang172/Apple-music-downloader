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


def eapi_url(song_id, level, encode):
    params = {"ids": f"[{song_id}]", "level": level, "encodeType": encode, "realIP": na._REAL_IP}
    body = b"params=" + na._eapi_body("/api/song/enhance/player/url/v1", params)
    request = Request("https://music.163.com/eapi/song/enhance/player/url/v1", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("data") or []
    return items[0] if isinstance(items, list) and items else {}


def try_fetch(url, cookie_text=""):
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
    if cookie_text:
        headers["Cookie"] = cookie_text
        headers["Referer"] = "https://music.163.com/"
    with build_opener().open(Request(url, headers=headers), timeout=25) as response:
        return (f"status={response.status} type={response.headers.get('Content-Type')!r} "
                f"len={response.headers.get('Content-Length')!r} prefix={response.read(8)!r}")


for level, encode in (("exhigh", "flac"), ("lossless", "flac"), ("standard", "mp3")):
    try:
        item = eapi_url(34338105, level, encode)
    except Exception as error:
        out.write(f"[{level}] api {type(error).__name__}: {error}\n")
        continue
    url = item.get("url") or ""
    out.write(f"[{level}] type={item.get('type')} br={item.get('br')} urlhost={(url.split('/')[2] if url else 'none')}\n")
    if not url:
        continue
    for label, use_cookie in (("plain", False), ("cookie+referer", True)):
        try:
            out.write(f"[{level}/{label}] {try_fetch(url, cookie if use_cookie else '')}\n")
        except HTTPError as error:
            out.write(f"[{level}/{label}] HTTP {error.code}\n")
        except Exception as error:
            out.write(f"[{level}/{label}] {type(error).__name__}: {error}\n")

with open(r"D:\AMDL\handoff\netease-eapi-dl.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

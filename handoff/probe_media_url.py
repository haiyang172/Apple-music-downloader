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
params = {"ids": "[34338105]", "level": "exhigh", "encodeType": "flac", "realIP": na._REAL_IP}
body = b"params=" + na._eapi_body("/api/song/enhance/player/url/v1", params)
request = Request("https://music.163.com/eapi/song/enhance/player/url/v1", data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
    url = json.loads(response.read().decode("utf-8"))["data"][0]["url"]
with open(r"D:\AMDL\handoff\media-url.txt", "w", encoding="utf-8") as fh:
    fh.write(url.replace("http://", "https://", 1))
print("url written")

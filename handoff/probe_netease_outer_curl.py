import io
import json
import os
import shutil
import subprocess
import sys
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth as na

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = na.NeteaseAuth(path).load()
cookie = "; ".join(f"{name}={value}" for name, value in session._cookies)
out = io.StringIO()

# 1) outer/url redirect route (what the web player often uses).
class Follow(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        out.write(f"   outer redirect {code} -> {urlsplit(newurl).netloc}\n")
        return super().redirect_request(request, response, code, message, headers, newurl)


from urllib.parse import urlsplit

try:
    request = Request("https://music.163.com/song/media/outer/url?id=34338105",
                      headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0",
                               "Referer": "https://music.163.com/"})
    with build_opener(Follow()).open(request, timeout=20) as response:
        out.write(f"[outer] final status={response.status} url={response.geturl()[:90]}\n")
        out.write(f"[outer] prefix={response.read(8)!r}\n")
except Exception as error:
    out.write(f"[outer] {type(error).__name__}: {error}\n")

# 2) curl HTTP/2 vs HTTP/1.1 against a media URL.
params = {"ids": "[34338105]", "level": "exhigh", "encodeType": "flac", "realIP": na._REAL_IP}
body = b"params=" + na._eapi_body("/api/song/enhance/player/url/v1", params)
request = Request("https://music.163.com/eapi/song/enhance/player/url/v1", data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "Cookie": cookie, "User-Agent": "Mozilla/5.0"})
with build_opener(na._NoRedirect()).open(request, timeout=15) as response:
    media = json.loads(response.read().decode("utf-8"))["data"][0]["url"].replace("http://", "https://", 1)

curl = shutil.which("curl")
out.write(f"curl={curl}\n")
if curl:
    for label, extra in (("http2", ["--http2"]), ("http1.1", ["--http1.1"])):
        try:
            result = subprocess.run([curl, "-s", "-o", "NUL", "-w", "%{http_code} %{size_download}",
                                     *extra, "--max-time", "25", media],
                                    capture_output=True, text=True, timeout=40)
            out.write(f"[curl {label}] {result.stdout.strip()} {result.stderr.strip()[:120]}\n")
        except Exception as error:
            out.write(f"[curl {label}] {type(error).__name__}: {error}\n")

with open(r"D:\AMDL\handoff\netease-outer-curl.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

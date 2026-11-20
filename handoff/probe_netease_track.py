import io
import json
import os
import sys

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth
from netease_auth import netease_api_request

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = netease_auth.NeteaseAuth(path).load()
out = io.StringIO()
out.write(f"session: {'yes' if session else 'none'}\n")
if session is not None:
    account = netease_auth.NeteaseAuth(path).check(session)
    out.write(f"account: {getattr(account, 'name', None)} uid={getattr(account, 'uid', None)}\n")
    song_id = 34338105
    for label, route, params in (
        ("auto br=999000", "/api/song/enhance/player/url", {"id": song_id, "ids": f"[{song_id}]", "br": 999000}),
        ("v1 exhigh", "/api/song/enhance/player/url/v1",
         {"ids": f"[{song_id}]", "level": "exhigh", "encodeType": "flac"}),
        ("v1 standard", "/api/song/enhance/player/url/v1",
         {"ids": f"[{song_id}]", "level": "standard", "encodeType": "mp3"}),
    ):
        try:
            reply = netease_api_request(session, route, params)
        except Exception as error:
            out.write(f"\n[{label}] ERROR {type(error).__name__}: {error}\n")
            continue
        items = reply.get("data")
        out.write(f"\n[{label}] code={reply.get('code')}\n")
        if isinstance(items, list) and items:
            item = items[0]
            keep = {k: item.get(k) for k in ("id", "code", "type", "level", "encodeType", "br", "size",
                                             "md5", "url", "freeTrialInfo", "fee", "payed")}
            out.write(json.dumps(keep, ensure_ascii=False, indent=1) + "\n")
        else:
            out.write(json.dumps(reply, ensure_ascii=False)[:800] + "\n")
    detail = netease_api_request(session, "/api/v3/song/detail",
                                 {"c": json.dumps([{"id": song_id}], separators=(",", ":"))})
    songs = detail.get("songs") or []
    if songs:
        s = songs[0]
        out.write("\n[detail] " + json.dumps({k: s.get(k) for k in ("id", "name", "fee", "st", "dt", "privilege")}, ensure_ascii=False)[:900] + "\n")
    else:
        out.write("\n[detail] no songs, code=" + str(detail.get("code")) + "\n")
with open(r"D:\AMDL\handoff\netease-probe.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

import os
import sys

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth as na

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = na.NeteaseAuth(path).load()
for level in ("standard", "exhigh", "lossless"):
    try:
        reply = na.netease_api_request(session, "/api/song/enhance/player/url/v1",
                                       {"ids": "[34338105]", "level": level, "encodeType": "flac"})
    except Exception as error:
        print(f"{level}: {type(error).__name__}: {error}")
        continue
    items = reply.get("data") or []
    item = items[0] if isinstance(items, list) and items else {}
    print(f"{level}: code={reply.get('code')} item={item.get('code')} type={item.get('type')} "
          f"br={item.get('br')} url={'yes' if item.get('url') else 'none'}")

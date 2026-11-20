import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_auth
import netease_download

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "netease-music-session.dat")
session = netease_auth.NeteaseAuth(path).load()
assert session is not None, "no saved session"

ffmpeg = r"D:\AMDL\backend\tools\ffmpeg.exe"
stop = threading.Event()
lines = []


def status(text):
    lines.append("status: " + text)


with tempfile.TemporaryDirectory(prefix="amdl-netease-") as scratch:
    try:
        target = netease_download.download_netease(
            "https://music.163.com/song?id=34338105", scratch, ffmpeg, stop, status,
            expected_duration=157.199, session=session, source_quality="auto")
        size = Path(target).stat().st_size
        lines.append(f"OK {target.name} {size} bytes")
    except Exception as error:
        lines.append(f"FAIL {type(error).__name__}: {error}")

with open(r"D:\AMDL\handoff\netease-download-probe.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("done")

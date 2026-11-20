import io
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import netease_download

stop = threading.Event()


def http_error(code):
    return HTTPError("https://m1.music.126.net/x.flac", code, "err", {}, io.BytesIO(b""))


with tempfile.TemporaryDirectory(prefix="amdl-err-") as stage:
    target = Path(stage) / "x.flac"
    cases = (
        (403, "媒体服务器拒绝了当前网络"),
        (401, "登录已失效"),
        (404, None),
        (500, "读取网易云音乐音轨失败"),
    )
    for code, fragment in cases:
        with patch.object(netease_download, "_open", side_effect=http_error(code)):
            try:
                with target.open("xb") as stream:
                    netease_download._copy_media("https://m1.music.126.net/x.flac", stream, stop,
                                                 lambda _: None, expected_size=10, checksum="0" * 32,
                                                 media_type="flac")
            except RuntimeError as error:
                text = str(error)
            else:
                raise AssertionError(f"HTTP {code} did not raise")
        if fragment is not None and fragment not in text:
            raise AssertionError(f"HTTP {code} message unexpected: {text}")
        print(f"HTTP {code}: {text}")
        target.unlink(missing_ok=True)
print("netease error mapping OK")

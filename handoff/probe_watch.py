import sys, tempfile
from pathlib import Path
sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")
from playlist_watch import PlaylistWatch

with tempfile.TemporaryDirectory(prefix="amdl-watch-") as scratch:
    path = Path(scratch) / "playlist-watch.json"
    watch = PlaylistWatch(path).load()
    rows = [
        {"url": "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qxYhV", "title": "A"},
        {"url": "https://music.163.com/song?id=1973665667", "title": "B"},
    ]
    new, complete = watch.diff("qq:123", rows)
    assert len(new) == 2 and len(complete) == 2
    watch.commit("qq:123", complete)
    again, complete2 = watch.diff("qq:123", rows + [{"url": "https://music.163.com/song?id=186016", "title": "C"}])
    assert len(again) == 1 and again[0]["title"] == "C" and len(complete2) == 3
    reloaded = PlaylistWatch(path).load()
    assert reloaded.diff("qq:123", rows)[0] == []
    assert reloaded.diff("qq:123", rows + [{"url": "https://music.163.com/song?id=186016", "title": "C"}])[0][0]["title"] == "C"
    reloaded.forget("qq:123")
    assert PlaylistWatch(path).load().diff("qq:123", rows)[0] == rows
    try:
        PlaylistWatch(path).diff("bad key!", rows)
        raise AssertionError("bad batch key accepted")
    except ValueError:
        pass
    print("playlist_watch OK: diff, commit, reload, forget and validation passed.")

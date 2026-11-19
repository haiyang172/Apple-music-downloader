import sys, tempfile, threading
from pathlib import Path
sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")
import dedup_review

with tempfile.TemporaryDirectory(prefix="amdl-dedup-") as scratch:
    root = Path(scratch)
    (root / "sub").mkdir()
    data = b"audio-bytes-" * 1000
    (root / "a.m4a").write_bytes(data)
    (root / "sub" / "b.m4a").write_bytes(data)
    (root / "c.flac").write_bytes(b"different")
    (root / "d.txt").write_bytes(data)  # not audio, ignored
    stop = threading.Event()
    result = dedup_review.scan_folder(root, stop=stop)
    assert result["scanned"] == 3 and result["duplicates"] == 1
    group = result["groups"][0]
    assert len(group["files"]) == 2 and group["size"] == len(data)
    removed, failures = dedup_review.delete_duplicates(
        [(group["files"][1], group["fingerprint"])], stop=stop)
    assert len(removed) == 1 and not failures
    assert not Path(group["files"][1]).exists() and Path(group["files"][0]).exists()
    # changed file is protected
    (root / "sub" / "b.m4a").write_bytes(b"changed")
    removed2, failures2 = dedup_review.delete_duplicates(
        [(str(root / "sub" / "b.m4a"), group["fingerprint"])], stop=stop)
    assert not removed2 and len(failures2) == 1 and "已更改" in failures2[0]
    print("dedup_review OK: scan, group, delete and change-guard passed.")

import sys, tempfile
from pathlib import Path
sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")
from unittest.mock import patch
import netease_auth
from netease_auth import NeteaseAccount

with tempfile.TemporaryDirectory(prefix="amdl-cookie-") as scratch:
    auth = netease_auth.NeteaseAuth(str(Path(scratch) / "netease.dat"))
    with patch.object(netease_auth, "_account", return_value=NeteaseAccount(12345, "测试")):
        session = auth.login_with_cookies("MUSIC_U=abc123; __csrf=def")
        assert session.user_id == 12345
        assert dict(session._cookies) == {"MUSIC_U": "abc123", "__csrf": "def"}
        assert auth.load() == session
        session2 = auth.login_with_cookies("abc123")
        assert dict(session2._cookies) == {"MUSIC_U": "abc123"}
    try:
        auth.login_with_cookies("bad cookie")
    except (ValueError, netease_auth.NeteaseAuthError):
        pass
    else:
        raise AssertionError("invalid cookie accepted")
    print("netease cookie login OK: full, bare and invalid inputs handled.")

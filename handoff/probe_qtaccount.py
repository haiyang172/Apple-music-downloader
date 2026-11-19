import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
with tempfile.TemporaryDirectory(prefix="amdl-qtacct-") as scratch:
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        os.environ[key] = scratch
    sys.path.insert(0, r"D:\AMDL")
    sys.path.insert(0, r"D:\AMDL\lib")
    import settings
    settings.DEFAULTS["auto_service"] = False
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from unittest.mock import patch
    from types import SimpleNamespace
    from netease_auth import NeteaseAuth, NeteaseSession, NeteaseAccount, NeteasePoll
    from qtui.account import MusicAccountDialog

    saved = {}
    fake_qr = SimpleNamespace(url="https://music.163.com/login?codekey=fixture")
    session = NeteaseSession(12345, (("MUSIC_U", "fixture-cookie"),))
    with patch.object(NeteaseAuth, "load", return_value=None), \
         patch.object(NeteaseAuth, "begin", return_value=fake_qr), \
         patch.object(NeteaseAuth, "poll", return_value=NeteasePoll("success", session)), \
         patch.object(NeteaseAuth, "save", side_effect=lambda s: saved.update(session=s)), \
         patch.object(NeteaseAuth, "check", return_value=NeteaseAccount(12345, "测试用户")):
        dialog = MusicAccountDialog(None, "netease")
        assert dialog.polling
        dialog.poll()
        assert not dialog.polling
        assert dialog.session == session
        assert saved.get("session") == session
        assert dialog.qr_label.text() == "✓ 已登录"
        assert "测试用户" in dialog.status.text()
    print("Qt account dialog OK: QR begin, poll success and session saved.")

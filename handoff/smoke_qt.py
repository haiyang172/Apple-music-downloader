import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
with tempfile.TemporaryDirectory(prefix="amdl-qt-smoke-") as scratch:
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        os.environ[key] = scratch
    sys.path.insert(0, r"D:\AMDL")
    import StudioQt
    window = StudioQt.build_window(dark=False)
    assert window.windowTitle() == "影音下载台"
    assert list(window.buttons) == ["music", "bilibili", "qq", "netease", "youtube", "douyin",
                                    "queue", "local", "settings"]
    window.show_page("queue")
    assert window.stack.currentWidget() is window.pages["queue"]
    window.show_page("settings")
    window.pages["settings"].toggle_theme()
    assert window.pages["settings"].dark is True
    window.pages["settings"].toggle_theme()
    assert window.pages["settings"].dark is False
    for key in ("music", "bilibili", "qq", "netease", "youtube", "douyin", "local"):
        window.show_page(key)
        assert window.stack.currentWidget() is window.pages[key]
    print("Qt shell smoke OK: nav, queue page, theme toggle and placeholders passed.")
    window.close()

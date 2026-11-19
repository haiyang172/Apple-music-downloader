import os, sys, tempfile, tkinter as tk
sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")
with tempfile.TemporaryDirectory(prefix="amdl-dark-") as scratch:
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        os.environ[key] = scratch
    import Studio
    import settings
    settings.DEFAULTS["auto_service"] = False
    import app_update
    app_update.check_update = lambda version: app_update.UpdateCheck("current", version)
    Studio.configure_windows()
    settings.save({**settings.load(), "dark_mode": True})
    window = tk.Tk()
    window.withdraw()
    app = Studio.Studio(window)
    assert app.dark is True
    assert app.background == "#12151c", app.background
    assert Studio.BG == "#12151c" and Studio.PANEL == "#1b2029" and Studio.INK == "#e6eaf2"
    window.deiconify()
    window.update_idletasks()
    app.close()
    # light default unchanged
    settings.save({**settings.load(), "dark_mode": False})
    Studio.apply_palette(False)
    assert Studio.BG == "#eff5f2" and Studio.PANEL == "#ffffff"
    print("Tkinter dark mode OK: palette applies at startup and light restores.")

"""Run only the real tag-edit portion of the install smoke test from this checkout."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import tkinter as tk


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="amdl-tag-handoff-") as scratch:
        profile = Path(scratch)
        for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            os.environ[key] = scratch
        import Studio
        import app_update
        import settings

        settings.DEFAULTS["auto_service"] = False
        app_update.check_update = lambda version: app_update.UpdateCheck("current", version)
        Studio.configure_windows()
        backend = Studio.find_backend()
        assert backend == root / "backend"
        audio = profile / "test-tone.m4a"
        subprocess.run([str(backend / "tools/ffmpeg.exe"), "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=0.1", "-c:a", "aac", str(audio)],
                       check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        test = root / "installer/Test-Installer.ps1"
        body = test.read_text(encoding="utf-8-sig").split("$smokeCode = @'", 1)[1].split("\n'@", 1)[0]
        helpers = body[body.index("def new_window():"):body.index("Studio.configure_windows()")]
        start = body.index("\nimport tag_edit\nfrom music_tagging import write_music_tags")
        if "Five audio tag formats" not in body[start:]:
            raise RuntimeError("Tag test section not found in Test-Installer.ps1")
        context = {**globals(), **locals()}
        exec(compile(helpers + body[start:], str(test) + " [tag section]", "exec"), context)


if __name__ == "__main__":
    main()

import io
import os
import sqlite3

path = os.path.join(os.environ["LOCALAPPDATA"], "amdl", "data", "downloads.sqlite3")
db = sqlite3.connect(path)
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT id, source, url, title, artist, state, error, options, batch_title "
    "FROM tasks ORDER BY id DESC LIMIT 15"
).fetchall()
out = io.StringIO()
for r in rows:
    out.write(f"#{r['id']}\n")
    out.write(f"  state   : {r['state']}\n")
    out.write(f"  source  : {r['source']}\n")
    out.write(f"  title   : {r['title']}\n")
    out.write(f"  artist  : {r['artist']}\n")
    out.write(f"  url     : {r['url']}\n")
    out.write(f"  batch   : {r['batch_title']}\n")
    out.write(f"  options : {r['options']}\n")
    out.write(f"  error   : {r['error']}\n\n")
with open(r"D:\AMDL\handoff\queue-dump.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("wrote", len(rows), "rows")

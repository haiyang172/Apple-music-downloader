import io
import sys
from urllib.request import Request, build_opener

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

out = io.StringIO()
targets = (
    ("ipip", "https://myip.ipip.net"),
    ("ipinfo", "https://ipinfo.io/json"),
    ("ip-api", "http://ip-api.com/json/?fields=query,country,regionName,city,isp"),
)
for label, url in targets:
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with build_opener().open(request, timeout=12) as response:
            body = response.read(800).decode("utf-8", "replace").strip()
        out.write(f"[{label}] {body}\n")
    except Exception as error:
        out.write(f"[{label}] {type(error).__name__}: {error}\n")
with open(r"D:\AMDL\handoff\egress.txt", "w", encoding="utf-8") as fh:
    fh.write(out.getvalue())
print("done")

import os
import sys
import urllib.request

sys.path.insert(0, r"D:\AMDL")
sys.path.insert(0, r"D:\AMDL\lib")

import net_proxy

assert net_proxy.apply({"proxy": "http://127.0.0.1:56590"}) == "http://127.0.0.1:56590"
assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:56590"
assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:56590"

try:
    urllib.request.build_opener().open("https://music.163.com/", timeout=5)
except Exception as error:
    used = "refused" in str(error).lower() or "10061" in str(error) or "proxy" in str(error).lower()
    assert used, f"urllib did not use the proxy: {type(error).__name__}: {error}"
    print("urllib honours env proxy:", type(error).__name__)
else:
    raise AssertionError("unexpected success through a dead proxy")

assert net_proxy.apply({"proxy": ""}) == ""
assert "HTTPS_PROXY" not in os.environ and "HTTP_PROXY" not in os.environ
assert net_proxy.valid("http://127.0.0.1:7890") and net_proxy.valid("https://10.0.0.2:8080/")
assert not net_proxy.valid("socks5://127.0.0.1:7890")
assert net_proxy.apply({"proxy": "socks5://bad"}) == "" and "HTTPS_PROXY" not in os.environ
print("net_proxy OK: set, clear, validation and urllib integration")

"""Route the app's HTTP stack through a user-configured proxy.

Every network module builds its urllib opener per request with the default
handler set, and child tools such as yt-dlp read the same variables, so setting
HTTP_PROXY/HTTPS_PROXY once at startup covers the whole application.
"""

import os
import re

import settings

_PROXY = re.compile(r"https?://[^\s/:]+(?::[0-9]{1,5})?/?")
_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def value(values=None):
    """Return the configured proxy URL, or an empty string when unset."""
    try:
        data = values if values is not None else settings.load()
    except (OSError, ValueError):
        return ""
    proxy = data.get("proxy")
    return proxy.strip() if isinstance(proxy, str) else ""


def valid(proxy):
    return bool(_PROXY.fullmatch(proxy or ""))


def apply(values=None):
    """Apply the configured proxy; an empty setting leaves the environment alone."""
    proxy = value(values)
    if proxy and not valid(proxy):
        return ""
    if not proxy:
        for name in _NAMES:
            os.environ.pop(name, None)
        return ""
    for name in _NAMES:
        os.environ[name] = proxy
    return proxy

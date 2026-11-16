"""Find supported links in copied share text without making requests."""

import re


_SHARE_URL = re.compile(r"(?<![A-Za-z0-9_/%.=-])https?://[^\s<>\"“”‘’「」『』《》〈〉【】（）。，、；：！？]+", re.I)
_TRAILING = "'.,?!;:)]}"


def links_in_text(value, accepts):
    """Keep a directly accepted value or find accepted URLs in surrounding text."""
    value = value.strip()
    if value and accepts(value):
        return (value,)
    urls = (match.group().rstrip(_TRAILING) for match in _SHARE_URL.finditer(value))
    return tuple(dict.fromkeys(url for url in urls if accepts(url)))

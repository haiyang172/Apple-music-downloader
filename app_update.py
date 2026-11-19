"""Check the project's public GitHub Releases for a newer stable version."""

from dataclasses import dataclass
from http.client import HTTPException
import json
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = "haiyang172/Media-Download-Desk"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
_LATEST_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
_MAX_RELEASE_BYTES = 1024 * 1024
_NUMBER = r"(0|[1-9][0-9]*)"
_VERSION = re.compile(rf"v?{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:\.{_NUMBER})?")


class UpdateCheckError(RuntimeError):
    """The public release could not be checked this time."""


@dataclass(frozen=True)
class UpdateCheck:
    status: str
    version: str | None = None
    url: str | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _version_key(version):
    if isinstance(version, str) and len(version) <= 64 and (match := _VERSION.fullmatch(version)):
        return tuple(int(part or 0) for part in match.groups())
    return None


def check_update(current_version):
    """Return available/current/unreleased after reading the public release metadata."""
    current = _version_key(current_version)
    if current is None:
        raise UpdateCheckError("无法识别当前程序版本。")

    request = Request(_LATEST_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"MediaDownloadDesk/{current_version}",
    })
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            payload = response.read(_MAX_RELEASE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        headers = error.headers or {}
        error.close()
        if status == 404:
            return UpdateCheck("unreleased")
        if status == 429 or (status == 403 and (headers.get("X-RateLimit-Remaining") == "0"
                                                or headers.get("Retry-After"))):
            raise UpdateCheckError("GitHub 更新查询过于频繁，请稍后再试。") from None
        raise UpdateCheckError("暂时无法读取 GitHub 版本信息。") from None
    except (OSError, HTTPException):
        raise UpdateCheckError("暂时无法连接 GitHub 检查更新。") from None

    if len(payload) > _MAX_RELEASE_BYTES:
        raise UpdateCheckError("GitHub 返回的版本信息过大。")
    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise UpdateCheckError("GitHub 返回的版本信息无法读取。") from None
    if (not isinstance(release, dict) or not isinstance(release.get("tag_name"), str)
            or not isinstance(release.get("draft"), bool) or not isinstance(release.get("prerelease"), bool)):
        raise UpdateCheckError("GitHub 返回的版本信息不完整。")

    tag = release["tag_name"]
    remote = _version_key(tag)
    if release["draft"] or release["prerelease"] or remote is None:
        return UpdateCheck("unreleased")
    version = tag.removeprefix("v")
    if remote <= current:
        return UpdateCheck("current", version)
    return UpdateCheck("available", version, f"{RELEASES_URL}/tag/{tag}")

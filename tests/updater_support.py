"""Fakes for the updater tests: an in-memory HTTP opener and fake yt-dlp
"exes" (small Python scripts run through sys.executable). No network."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.error
from pathlib import Path

from core import updater

TAG = "2025.10.22"
EXE_URL = f"{updater.DOWNLOAD_PREFIX}{TAG}/yt-dlp.exe"
SUMS_URL = f"{updater.DOWNLOAD_PREFIX}{TAG}/SHA2-256SUMS"


def script(version_line: str, padding: int = 0, exit_code: int = 0, sleep: float = 0.0) -> bytes:
    """A fake yt-dlp: prints ``version_line`` for --version."""
    lines = ["import sys, time"]
    if sleep:
        lines.append(f"time.sleep({sleep})")
    lines.append(f"print({version_line!r})")
    lines.append(f"sys.exit({exit_code})")
    body = "\n".join(lines) + "\n"
    if padding:
        body += "#" + "x" * padding + "\n"
    return body.encode()


def run_with_python(exe: Path) -> list[str]:
    return [sys.executable, str(exe)]


def write_exe(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(script(version))
    return path


class FakeResponse:
    def __init__(self, data: bytes, url: str, delay: float = 0.0, gate: threading.Event | None = None) -> None:
        self._data = data
        self._pos = 0
        self._url = url
        self._delay = delay
        self._gate = gate
        self.headers = {"Content-Length": str(len(data))}
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._gate is not None and self._pos > 0:
            self._gate.wait(self._delay or 5)
        elif self._delay:
            threading.Event().wait(self._delay)
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """URL -> bytes, or an exception to raise. ``final_urls`` fakes redirects."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = dict(routes or {})
        self.final_urls: dict[str, str] = {}
        self.requested: list[str] = []
        self.delay: dict[str, float] = {}
        self.gate: threading.Event | None = None  # blocks exe chunks after the first

    def open(self, request, timeout=None):  # noqa: ARG002
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.requested.append(url)
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        gate = self.gate if url == EXE_URL else None
        return FakeResponse(value, self.final_urls.get(url, url), self.delay.get(url, 0.0), gate)


def release_json(tag: str = TAG, exe_url: str = EXE_URL, sums_url: str = SUMS_URL, size: int | None = None) -> bytes:
    exe = {"name": "yt-dlp.exe", "browser_download_url": exe_url}
    if size is not None:
        exe["size"] = size
    return json.dumps(
        {"tag_name": tag, "assets": [exe, {"name": "SHA2-256SUMS", "browser_download_url": sums_url}]}
    ).encode()


def sums_for(data: bytes, name: str = "yt-dlp.exe") -> bytes:
    digest = hashlib.sha256(data).hexdigest()
    other = hashlib.sha256(b"other").hexdigest()
    return f"{other}  yt-dlp\n{digest}  {name}\n{other}  yt-dlp_macos\n".encode()


def release_opener(new_exe: bytes, tag: str = TAG, sums: bytes | None = None) -> FakeOpener:
    return FakeOpener(
        {
            updater.LATEST_RELEASE_URL: release_json(tag, size=len(new_exe)),
            SUMS_URL: sums if sums is not None else sums_for(new_exe),
            EXE_URL: new_exe,
        }
    )

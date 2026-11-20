"""Update yt-dlp.exe from its official GitHub releases (no UI, no Qt).

Steps: ask GitHub for the latest release -> download ``yt-dlp.exe`` to a
temp file in the tools folder -> check its SHA-256 against the release's
SHA2-256SUMS -> run ``--version`` on it -> back up the current copy to
``yt-dlp.exe.bak`` -> replace atomically.

On any failure the old working file stays (or is restored from the
backup), the temp file is deleted and a plain-English result is returned.
The network (``opener``), folders and the command used to run an exe are
injected so tests never touch the real system.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from core.converter import CREATE_NO_WINDOW, child_env, kill_process_tree

REPO = "yt-dlp/yt-dlp"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
EXE_NAME = "yt-dlp.exe"
BACKUP_NAME = "yt-dlp.exe.bak"
SUMS_NAME = "SHA2-256SUMS"
USER_AGENT = "yt-mp3-converter-updater"

ALLOWED_HOSTS = frozenset({"github.com", "api.github.com"})
ALLOWED_HOST_SUFFIX = ".githubusercontent.com"

NETWORK_TIMEOUT_S = 30
VERSION_TIMEOUT_S = 60
CHUNK_BYTES = 64 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SUMS_BYTES = 256 * 1024
MAX_EXE_BYTES = 300 * 1024 * 1024

_VERSION = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}(\.\d+)*$")

FAILED_TEXT = "Couldn't update. Your current version still works."
FAILED_INSTALL_TEXT = "Couldn't install the download component. Check your internet connection and try again."


class UpdateKind(str, Enum):
    UPDATED = "updated"
    ALREADY_LATEST = "already_latest"
    INSTALLED = "installed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class UpdateResult:
    kind: UpdateKind
    version: str = ""
    message: str = ""  # plain English (FAILED only)
    details: str = ""  # raw technical text, for a tooltip only


class Stage(str, Enum):
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    INSTALLING = "installing"


# stage, bytes done (DOWNLOADING only), total bytes (None = unknown)
ProgressCallback = Callable[[Stage, int, "int | None"], None]


class Response(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def close(self) -> None: ...


class Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float = ...) -> Response: ...


class UpdateError(Exception):
    """A failed step; ``str()`` is the technical detail for the tooltip."""


class Cancelled(Exception):
    pass


# --- URLs ---------------------------------------------------------------------


def is_allowed_url(url: str) -> bool:
    """HTTPS on github.com / api.github.com / *.githubusercontent.com only."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if parts.port not in (None, 443):
        return False
    return host in ALLOWED_HOSTS or (host.endswith(ALLOWED_HOST_SUFFIX) and len(host) > len(ALLOWED_HOST_SUFFIX))


def is_official_asset_url(url: str, name: str) -> bool:
    """A file of a release of the official yt-dlp repository."""
    if not is_allowed_url(url) or not url.startswith(DOWNLOAD_PREFIX):
        return False
    tag_and_name = url[len(DOWNLOAD_PREFIX):].split("/")
    return len(tag_and_name) == 2 and bool(tag_and_name[0]) and tag_and_name[1] == name


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to allowed HTTPS hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if not is_allowed_url(newurl):
            raise urllib.error.URLError(f"blocked redirect to {urlsplit(newurl).hostname or newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener() -> Opener:
    import ssl

    context = ssl.create_default_context()
    return urllib.request.build_opener(SafeRedirectHandler(), urllib.request.HTTPSHandler(context=context))


# --- versions -----------------------------------------------------------------


def is_version(text: str) -> bool:
    return bool(_VERSION.match(text.strip()))


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.strip().split("."))


def default_command(exe: Path) -> list[str]:
    return [str(exe)]


def read_version(
    exe: Path,
    command_for: Callable[[Path], list[str]] = default_command,
    timeout: float = VERSION_TIMEOUT_S,
    cancel: threading.Event | None = None,
) -> str | None:
    """Run ``<exe> --version`` without a console window; None if it fails,
    times out, is cancelled or prints something that is not a version."""
    try:
        process = subprocess.Popen(
            [*command_for(Path(exe)), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            env=child_env(),
        )
    except (OSError, ValueError):
        return None
    deadline = time.monotonic() + timeout
    output = b""
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                    kill_process_tree(process)
                    process.communicate()
                    return None
    finally:
        if process.poll() is None:
            kill_process_tree(process)
    if process.returncode != 0:
        return None
    lines = output.decode("utf-8", errors="replace").strip().splitlines()
    version = lines[-1].strip() if lines else ""
    return version if is_version(version) else None


# --- network helpers ----------------------------------------------------------


def _request(url: str, accept: str = "*/*") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def _open(opener: Opener, url: str, accept: str = "*/*") -> Response:
    if not is_allowed_url(url):
        raise UpdateError(f"refusing to open {url}")
    response = opener.open(_request(url, accept), timeout=NETWORK_TIMEOUT_S)
    final = response.geturl() or url
    if not is_allowed_url(final):
        response.close()
        raise UpdateError(f"redirected to a site that is not allowed: {urlsplit(final).hostname}")
    return response


def _read_small(opener: Opener, url: str, limit: int, accept: str = "*/*") -> bytes:
    response = _open(opener, url, accept)
    try:
        data = response.read(limit + 1)
    finally:
        response.close()
    if len(data) > limit:
        raise UpdateError(f"response from {url} is too large")
    return data


@dataclass(frozen=True)
class Release:
    version: str
    exe_url: str
    sums_url: str
    exe_size: int | None


def fetch_latest_release(opener: Opener) -> Release:
    data = _read_small(opener, LATEST_RELEASE_URL, MAX_JSON_BYTES, "application/vnd.github+json")
    try:
        info = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise UpdateError(f"unreadable release information: {exc}") from exc
    if not isinstance(info, dict):
        raise UpdateError("unreadable release information")
    tag = str(info.get("tag_name") or "").strip()
    if not is_version(tag):
        raise UpdateError(f"unexpected release tag: {tag[:40]!r}")
    assets = {
        asset.get("name"): asset
        for asset in info.get("assets") or []
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }
    exe, sums = assets.get(EXE_NAME), assets.get(SUMS_NAME)
    if exe is None or sums is None:
        raise UpdateError("the latest release has no yt-dlp.exe or SHA2-256SUMS")
    exe_url = str(exe.get("browser_download_url") or "")
    sums_url = str(sums.get("browser_download_url") or "")
    if not is_official_asset_url(exe_url, EXE_NAME) or not is_official_asset_url(sums_url, SUMS_NAME):
        raise UpdateError("release files are not on the official yt-dlp page")
    size = exe.get("size")
    return Release(tag, exe_url, sums_url, size if isinstance(size, int) and size > 0 else None)


def parse_sums(text: str, name: str = EXE_NAME) -> str | None:
    """The SHA-256 for ``name`` in a ``sha256sum``-style file."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def download_to(
    opener: Opener,
    url: str,
    target: Path,
    expected_size: int | None,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> str:
    """Stream ``url`` into ``target``; return the SHA-256 hex digest."""
    response = _open(opener, url, "application/octet-stream")
    digest = hashlib.sha256()
    done = 0
    try:
        total = expected_size
        length = getattr(response, "headers", None)
        if length is not None:
            value = length.get("Content-Length")
            if value and str(value).isdigit() and int(value) > 0:
                total = int(value)
        progress(Stage.DOWNLOADING, 0, total)
        with open(target, "wb") as file:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_EXE_BYTES:
                    raise UpdateError("download is larger than expected")
                digest.update(chunk)
                file.write(chunk)
                progress(Stage.DOWNLOADING, done, total)
    finally:
        response.close()
    if cancel.is_set():
        raise Cancelled()
    if expected_size is not None and done != expected_size:
        raise UpdateError(f"download incomplete: {done} of {expected_size} bytes")
    return digest.hexdigest()


# --- the update ---------------------------------------------------------------


def _no_progress(_stage: Stage, _done: int, _total: int | None) -> None:
    pass


@dataclass
class UpdateRequest:
    """Everything one update needs.

    tools_dir: folder that receives ``yt-dlp.exe`` (``%LOCALAPPDATA%\\...\\tools``).
    current_exe: the yt-dlp the app uses now, or None if there is none anywhere.
    """

    tools_dir: Path
    current_exe: Path | None
    opener: Opener
    command_for: Callable[[Path], list[str]] = default_command
    progress: ProgressCallback = _no_progress
    cancel: threading.Event = field(default_factory=threading.Event)
    replace: Callable[[str | Path, str | Path], None] = os.replace
    version_timeout: float = VERSION_TIMEOUT_S


def _failed(installing: bool, details: str) -> UpdateResult:
    return UpdateResult(
        UpdateKind.FAILED,
        message=FAILED_INSTALL_TEXT if installing else FAILED_TEXT,
        details=details,
    )


def _remove(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def update_ytdlp(request: UpdateRequest) -> UpdateResult:
    """Check, download, verify and install the latest yt-dlp.exe."""
    tools_dir = Path(request.tools_dir)
    target = tools_dir / EXE_NAME
    backup = tools_dir / BACKUP_NAME
    current = Path(request.current_exe) if request.current_exe is not None else None
    installing = current is None
    cancel = request.cancel
    temp: Path | None = None
    backed_up = False
    try:
        request.progress(Stage.CHECKING, 0, None)
        current_version = (
            read_version(current, request.command_for, request.version_timeout, cancel) if current else None
        )
        if cancel.is_set():
            raise Cancelled()
        release = fetch_latest_release(request.opener)
        if cancel.is_set():
            raise Cancelled()
        if current_version and version_key(current_version) >= version_key(release.version):
            return UpdateResult(UpdateKind.ALREADY_LATEST, version=current_version)

        sums = _read_small(request.opener, release.sums_url, MAX_SUMS_BYTES)
        expected = parse_sums(sums.decode("utf-8", errors="replace"))
        if expected is None:
            raise UpdateError("SHA2-256SUMS has no entry for yt-dlp.exe")

        tools_dir.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=tools_dir, prefix="yt-dlp-", suffix=".download")
        os.close(handle)
        temp = Path(temp_name)
        actual = download_to(request.opener, release.exe_url, temp, release.exe_size, request.progress, cancel)
        request.progress(Stage.VERIFYING, 0, None)
        if actual != expected:
            raise UpdateError(f"SHA-256 mismatch: expected {expected}, got {actual}")
        new_version = read_version(temp, request.command_for, request.version_timeout, cancel)
        if cancel.is_set():
            raise Cancelled()
        if new_version is None:
            raise UpdateError("the downloaded yt-dlp.exe did not report a version")

        request.progress(Stage.INSTALLING, 0, None)
        if cancel.is_set():
            raise Cancelled()
        if target.exists():
            shutil.copy2(target, backup)
            backed_up = True
        request.replace(temp, target)
        temp = None
        kind = UpdateKind.INSTALLED if installing else UpdateKind.UPDATED
        return UpdateResult(kind, version=new_version)
    except Cancelled:
        return UpdateResult(UpdateKind.CANCELLED)
    except UpdateError as exc:
        return _failed(installing, str(exc))
    except urllib.error.HTTPError as exc:
        return _failed(installing, f"HTTP error {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return _failed(installing, f"network error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 - every failure must keep the old file
        return _failed(installing, f"{type(exc).__name__}: {exc}")
    finally:
        _remove(temp)
        if backed_up and not target.exists() and backup.exists():
            try:
                shutil.copy2(backup, target)
            except OSError:
                pass


def tools_busy(has_active: bool, running_workers: int) -> bool:
    """True while conversions are queued or a yt-dlp process may still run
    (Windows locks the running exe, so it must not be replaced)."""
    return has_active or running_workers > 0

"""Import and remove the optional cookies.txt (pure file logic, no UI).

The file holds login information: its contents are never printed, logged
or put into a message or exception text. Only its shape is checked.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core import tools

MAX_COOKIES_BYTES = 10 * 1024 * 1024
NETSCAPE_HEADERS = ("# netscape http cookie file", "# http cookie file")
HTTPONLY_PREFIX = "#HttpOnly_"
COOKIE_FIELDS = 7


class CookieStatus(str, Enum):
    IMPORTED = "imported"  # copy in the app data folder
    NEXT_TO_APP = "next_to_app"  # old fallback: cookies.txt beside the app
    NOT_IMPORTED = "not_imported"


@dataclass(frozen=True)
class CookieResult:
    ok: bool
    message: str  # plain English, never contains the file's contents


def imported_path(app_data: Path) -> Path:
    return Path(app_data) / tools.COOKIES_FILE


def cookie_status(app_data: Path, app_dir: Path) -> CookieStatus:
    if tools._is_file(imported_path(app_data)):
        return CookieStatus.IMPORTED
    if tools._is_file(Path(app_dir) / tools.COOKIES_FILE):
        return CookieStatus.NEXT_TO_APP
    return CookieStatus.NOT_IMPORTED


def _is_cookie_line(line: str) -> bool:
    if line.startswith(HTTPONLY_PREFIX):
        line = line[len("#HttpOnly_"):]
    elif line.startswith("#"):
        return False
    fields = line.split("\t")
    if len(fields) != COOKIE_FIELDS:
        return False
    domain, subdomains, _path, secure, expires = fields[:5]
    return (
        bool(domain)
        and subdomains.upper() in ("TRUE", "FALSE")
        and secure.upper() in ("TRUE", "FALSE")
        and expires.lstrip("-").isdigit()
    )


def looks_like_netscape_cookies(data: bytes) -> bool:
    """A Netscape/Mozilla cookies.txt: optional header comment, then at least
    one tab-separated line with 7 fields."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    lines = [line.rstrip("\r") for line in text.split("\n")]
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return False
    has_header = meaningful[0].strip().lower().startswith(NETSCAPE_HEADERS)
    cookie_lines = [line for line in meaningful if _is_cookie_line(line)]
    if not cookie_lines:
        return False
    # Without the header, every non-comment line must be a cookie line.
    if not has_header:
        others = [line for line in meaningful if not line.startswith("#") and not _is_cookie_line(line)]
        if others:
            return False
    return True


def import_cookies(source: Path, app_data: Path) -> CookieResult:
    """Check ``source`` and copy it to ``<app data>\\cookies.txt``."""
    source = Path(source)
    target = imported_path(app_data)
    try:
        if source.stat().st_size > MAX_COOKIES_BYTES:
            return CookieResult(False, "This file is too large to be a cookies file.")
        with open(source, "rb") as handle:
            data = handle.read(MAX_COOKIES_BYTES + 1)
    except OSError:
        return CookieResult(False, "Couldn't read this file. Choose another one.")
    if not looks_like_netscape_cookies(data):
        return CookieResult(False, "This doesn't look like a cookies.txt file. Choose another one.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.exists() and os.path.samefile(source, target):
                return CookieResult(True, "Cookies imported.")
        except OSError:
            pass
        handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix="cookies-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError:
        return CookieResult(False, "Couldn't save the cookies file. Please try again.")
    return CookieResult(True, "Cookies imported.")


def remove_cookies(app_data: Path) -> CookieResult:
    """Delete only the app data copy (never the file next to the app)."""
    target = imported_path(app_data)
    try:
        target.unlink()
    except FileNotFoundError:
        return CookieResult(True, "Cookies removed.")
    except OSError:
        return CookieResult(False, "Couldn't remove the cookies file. Close other programs and try again.")
    return CookieResult(True, "Cookies removed.")


"""Turn raw downloader/converter output into plain-language messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ErrorKind(str, Enum):
    NO_NETWORK = "no_network"
    UNAVAILABLE = "unavailable"
    SIGN_IN = "sign_in"
    CONVERTER_MISSING = "converter_missing"
    DISK_FULL = "disk_full"
    NO_PERMISSION = "no_permission"
    FILE_MISSING = "file_missing"  # a converted file vanished before saving
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FriendlyError:
    kind: ErrorKind
    message: str
    hint: str
    details: str  # raw text for a "Details" tooltip


_TEXT = {
    ErrorKind.NO_NETWORK: (
        "No internet connection",
        "Check that this computer is online, then press Retry.",
    ),
    ErrorKind.UNAVAILABLE: (
        "This video can't be opened",
        "It may be private, removed, or blocked in your country. Try a different link.",
    ),
    ErrorKind.SIGN_IN: (
        "YouTube asked to confirm you're not a robot",
        "Wait a few minutes and press Retry. If it keeps happening, import cookies in Settings.",
    ),
    ErrorKind.CONVERTER_MISSING: (
        "The MP3 converter is missing",
        "Reinstall the app, then try again.",
    ),
    ErrorKind.DISK_FULL: (
        "The computer is out of space",
        "Delete some files or choose another folder in Settings, then press Retry.",
    ),
    ErrorKind.NO_PERMISSION: (
        "Can't save to the MP3 folder",
        "Close any program using the file, or choose another folder in Settings, then press Retry.",
    ),
    ErrorKind.UNKNOWN: (
        "Something went wrong with this video",
        "Press Retry. If it keeps failing, click \"Downloads not working? Update\".",
    ),
}

# Checked in order; the first matching kind wins. Patterns are lowercase.
_PATTERNS: tuple[tuple[ErrorKind, tuple[str, ...]], ...] = (
    (
        ErrorKind.CONVERTER_MISSING,
        (
            "ffmpeg not found",
            "ffprobe not found",
            "ffprobe and ffmpeg not found",
            "ffmpeg and ffprobe not found",
            "ffmpeg is not installed",
            "ffmpeg-location",
        ),
    ),
    (
        ErrorKind.DISK_FULL,
        (
            "no space left on device",
            "not enough space on the disk",
            "errno 28",
            "winerror 112",
            "disk quota exceeded",
        ),
    ),
    (
        ErrorKind.NO_PERMISSION,
        (
            "permission denied",
            "access is denied",
            "errno 13",
            "winerror 32",
            "being used by another process",
            "read-only file system",
        ),
    ),
    (
        ErrorKind.SIGN_IN,
        (
            "sign in to confirm",
            "not a bot",
            "--cookies",
            "cookies",
            "login required",
            "sign in to view",
            "confirm your age",
            "age-restricted",
            "age restricted",
            "http error 429",
            "too many requests",
        ),
    ),
    (
        ErrorKind.UNAVAILABLE,
        (
            "video unavailable",
            "private video",
            "video is private",
            "has been removed",
            "video is not available",
            "this content isn't available",
            "no longer available",
            "account associated with this video has been terminated",
            "copyright",
            "not made this video available in your country",
            "members-only",
            "join this channel",
            "premieres in",
            "live event will begin",
            "http error 404",
            "incomplete youtube id",
        ),
    ),
    (
        ErrorKind.NO_NETWORK,
        (
            "getaddrinfo failed",
            "name or service not known",
            "temporary failure in name resolution",
            "failed to resolve",
            "nodename nor servname",
            "no address associated with hostname",
            "network is unreachable",
            "connection refused",
            "connection reset",
            "connection aborted",
            "remote end closed connection",
            "timed out",
            "winerror 10051",
            "winerror 10060",
            "winerror 10061",
            "winerror 11001",
            "unable to connect",
            "failed to establish a new connection",
            "urlopen error",
        ),
    ),
)


def clean_text(raw: str | None) -> str:
    return _ANSI.sub("", str(raw or "")).strip()


def classify_error(raw_text: str | None) -> FriendlyError:
    details = clean_text(raw_text)
    lowered = details.lower()
    kind = ErrorKind.UNKNOWN
    for candidate, patterns in _PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            kind = candidate
            break
    message, hint = _TEXT[kind]
    return FriendlyError(kind=kind, message=message, hint=hint, details=details)


def friendly_error(raw_text: str | None) -> tuple[str, str]:
    """Return ``(short_message, hint)`` for raw error output.

    Use ``classify_error`` when the raw text is also needed (``details``).
    """
    error = classify_error(raw_text)
    return error.message, error.hint


def missing_tool_error(tool_name: str) -> FriendlyError:
    """Error for a converter program that could not be found on this computer."""
    message, hint = _TEXT[ErrorKind.CONVERTER_MISSING]
    if tool_name == "yt-dlp":
        hint = "Click \"Downloads not working? Update\", or reinstall the app."
    return FriendlyError(
        kind=ErrorKind.CONVERTER_MISSING,
        message=message,
        hint=hint,
        details=f"{tool_name} was not found on this computer.",
    )

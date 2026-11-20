"""Parse pasted text into normalized YouTube video links.

The input is split on any whitespace, so one line may hold several links.
Every piece of text is reported either as a valid link or as a skipped link
with a reason, together with its position in the original text so a UI can
highlight it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

MAX_LINKS = 50

YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_TOKEN = re.compile(r"\S+")
# Characters that often wrap or trail a link copied from chats or documents.
_LEADING_JUNK = "<([\"'"
_TRAILING_JUNK = ">)]\"',;."
# Path prefixes whose next segment is the video ID.
_ID_PATH_PREFIXES = frozenset({"shorts", "live", "embed", "v", "e"})
_CHANNEL_PREFIXES = frozenset({"channel", "c", "user"})


class SkipReason(str, Enum):
    """Why a piece of input was not queued."""

    INVALID = "invalid"  # not a link, or a YouTube link without a video
    NOT_YOUTUBE = "not_youtube"
    PLAYLIST = "playlist"
    CHANNEL = "channel"
    ALBUM = "album"
    DUPLICATE = "duplicate"
    OVER_LIMIT = "over_limit"

    @property
    def is_input_error(self) -> bool:
        """True when the user must fix the line (it should stay highlighted)."""
        return self not in (SkipReason.DUPLICATE, SkipReason.OVER_LIMIT)


_MESSAGES = {
    SkipReason.INVALID: "Not a YouTube video link",
    SkipReason.NOT_YOUTUBE: "Only YouTube links are supported",
    SkipReason.PLAYLIST: "This is a playlist. Paste the links of single videos instead",
    SkipReason.CHANNEL: "This is a channel page. Paste the links of single videos instead",
    SkipReason.ALBUM: "This is an album or artist page. Paste the links of single songs instead",
    SkipReason.DUPLICATE: "Already in this list",
    SkipReason.OVER_LIMIT: f"Over the limit of {MAX_LINKS} links. Convert these in the next batch",
}


def skip_message(reason: SkipReason) -> str:
    return _MESSAGES[reason]


class LinkError(ValueError):
    """Raised by normalize_url when text is not a single YouTube video."""

    def __init__(self, reason: SkipReason) -> None:
        super().__init__(skip_message(reason))
        self.reason = reason


@dataclass(frozen=True)
class Span:
    """Where a piece of text sits in the original input.

    ``line`` is 0-based. ``start``/``end`` are character offsets into the
    whole input text (``end`` exclusive), so a UI can select them directly.
    """

    text: str
    line: int
    start: int
    end: int


@dataclass(frozen=True)
class ValidLink:
    span: Span
    video_id: str
    url: str


@dataclass(frozen=True)
class SkippedLink:
    span: Span
    reason: SkipReason
    message: str
    video_id: str | None = None


@dataclass(frozen=True)
class ParseResult:
    valid: tuple[ValidLink, ...]
    skipped: tuple[SkippedLink, ...]

    @property
    def urls(self) -> list[str]:
        return [link.url for link in self.valid]

    @property
    def input_errors(self) -> tuple[SkippedLink, ...]:
        """Skipped items the user has to fix (not duplicates or overflow)."""
        return tuple(item for item in self.skipped if item.reason.is_input_error)

    def skipped_counts(self) -> dict[SkipReason, int]:
        counts: dict[SkipReason, int] = {}
        for item in self.skipped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _clean_token(token: str) -> tuple[str, int]:
    """Strip wrapping punctuation. Returns the cleaned text and its offset."""
    stripped_front = token.lstrip(_LEADING_JUNK)
    offset = len(token) - len(stripped_front)
    return stripped_front.rstrip(_TRAILING_JUNK), offset


def _valid_id(candidate: str | None) -> str | None:
    if candidate and _VIDEO_ID.fullmatch(candidate):
        return candidate
    return None


def normalize_url(raw: str) -> str:
    """Return the canonical watch URL for one YouTube video link.

    Raises LinkError with a SkipReason when the text is not a single video.
    """
    return watch_url(extract_video_id(raw))


def extract_video_id(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise LinkError(SkipReason.INVALID)

    # Accept links pasted without a scheme, e.g. "youtu.be/abc" or "www.youtube.com/...".
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ("http", "https"):
        raise LinkError(SkipReason.INVALID)

    try:
        host = (parsed.hostname or "").lower()
    except ValueError:
        raise LinkError(SkipReason.INVALID) from None
    if not host or "." not in host:
        raise LinkError(SkipReason.INVALID)

    segments = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if host in SHORT_HOSTS:
        video_id = _valid_id(segments[0] if segments else None)
        if video_id is None:
            raise LinkError(SkipReason.INVALID)
        return video_id

    if host not in YOUTUBE_HOSTS:
        raise LinkError(SkipReason.NOT_YOUTUBE)

    first = segments[0].lower() if segments else ""

    if first == "watch":
        video_id = _valid_id((query.get("v") or [None])[0])
        if video_id:
            return video_id
        if query.get("list"):
            raise LinkError(SkipReason.PLAYLIST)
        raise LinkError(SkipReason.INVALID)

    if first in _ID_PATH_PREFIXES and len(segments) >= 2:
        video_id = _valid_id(segments[1])
        if video_id:
            return video_id
        raise LinkError(SkipReason.INVALID)

    if first == "playlist":
        raise LinkError(SkipReason.PLAYLIST)
    if first == "browse":
        raise LinkError(SkipReason.ALBUM)
    if first.startswith("@") or first in _CHANNEL_PREFIXES:
        raise LinkError(SkipReason.CHANNEL)

    raise LinkError(SkipReason.INVALID)


def _tokens(text: str):
    """Yield (Span) for every whitespace-separated piece of text."""
    line_starts = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
    line = 0
    for match in _TOKEN.finditer(text):
        while line + 1 < len(line_starts) and line_starts[line + 1] <= match.start():
            line += 1
        cleaned, offset = _clean_token(match.group())
        if not cleaned:
            cleaned, offset = match.group(), 0
        start = match.start() + offset
        yield Span(text=cleaned, line=line, start=start, end=start + len(cleaned))


def parse_links(text: str, limit: int = MAX_LINKS) -> ParseResult:
    """Split ``text`` into valid video links and skipped items.

    Order is preserved. Duplicates (same video ID) and links past ``limit``
    are skipped; the first occurrence of a video wins.
    """
    valid: list[ValidLink] = []
    skipped: list[SkippedLink] = []
    seen: set[str] = set()

    for span in _tokens(text):
        try:
            video_id = extract_video_id(span.text)
        except LinkError as exc:
            previous = skipped[-1] if skipped else None
            if (
                exc.reason is SkipReason.INVALID
                and previous is not None
                and previous.reason is SkipReason.INVALID
                and previous.span.line == span.line
                and previous.span.end <= span.start
                and not (valid and valid[-1].span.start > previous.span.start)
            ):
                # Plain words such as "my song name" count as one bad line,
                # not one bad item per word.
                merged = Span(
                    text=text[previous.span.start:span.end],
                    line=span.line,
                    start=previous.span.start,
                    end=span.end,
                )
                skipped[-1] = SkippedLink(merged, SkipReason.INVALID, previous.message)
                continue
            skipped.append(SkippedLink(span, exc.reason, skip_message(exc.reason)))
            continue

        if video_id in seen:
            reason = SkipReason.DUPLICATE
        elif len(valid) >= limit:
            reason = SkipReason.OVER_LIMIT
        else:
            seen.add(video_id)
            valid.append(ValidLink(span, video_id, watch_url(video_id)))
            continue
        skipped.append(SkippedLink(span, reason, skip_message(reason), video_id))

    return ParseResult(tuple(valid), tuple(skipped))

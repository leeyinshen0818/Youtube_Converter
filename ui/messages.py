"""User-facing texts and small decisions, kept free of Qt so they are easy
to test: summary line, "All done" line, Clear/close confirmations."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from core.jobqueue import ItemSnapshot, ItemStatus
from core.saver import default_save_dir

SUMMARY_ORDER = (
    ItemStatus.DOWNLOADING,
    ItemStatus.CONVERTING,
    ItemStatus.WAITING,
    ItemStatus.READY,
    ItemStatus.SAVED,
    ItemStatus.FAILED,
    ItemStatus.STOPPED,
)


def count_text(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def save_location_label(save_dir: Path) -> str:
    """"Downloads" for the default folder (or any "...\\Downloads\\YouTube MP3
    Converter", e.g. a redirected Downloads folder), otherwise the folder's name."""
    save_dir = Path(save_dir)
    default = default_save_dir()
    try:
        if save_dir.resolve() == default.resolve():
            return "Downloads"
    except OSError:
        pass
    if save_dir.name == default.name and save_dir.parent.name.lower() == "downloads":
        return "Downloads"
    return save_dir.name or str(save_dir)


def summary_text(counts: Mapping[ItemStatus, int]) -> str:
    """e.g. "2 downloading · 3 waiting · 5 ready · 1 failed" (zeros hidden)."""
    return " · ".join(
        f"{counts[status]} {status.value}" for status in SUMMARY_ORDER if counts.get(status)
    )


def all_done_text(ready: int, saved: int = 0, failed: int = 0, stopped: int = 0) -> str:
    parts = []
    if ready:
        parts.append(f"{ready} ready to download")
    if saved:
        parts.append(f"{saved} saved")
    if failed:
        parts.append(f"{failed} failed")
    if stopped:
        parts.append(f"{stopped} stopped")
    return "All done: " + " · ".join(parts) if parts else "All done"


def saved_text(saved: int, failed: int, location: str) -> str:
    text = f"{count_text(saved, 'MP3', 'MP3s')} saved to {location}"
    if failed:
        text += f" · {failed} couldn't be saved"
    return text


def clear_confirmation_text(unsaved: int) -> str | None:
    """Question before Clear Finished, or None when nothing would be lost."""
    if unsaved <= 0:
        return None
    verb = "hasn't" if unsaved == 1 else "haven't"
    return f"{count_text(unsaved, 'MP3', 'MP3s')} {verb} been downloaded yet. Clear anyway?"


def close_warning_text(running: int, unsaved: int) -> str | None:
    """What would be lost by quitting now, or None when nothing is at stake."""
    parts = []
    if running > 0:
        verb = "is" if running == 1 else "are"
        parts.append(f"{count_text(running, 'download', 'downloads')} {verb} still running")
    if unsaved > 0:
        verb = "hasn't" if unsaved == 1 else "haven't"
        parts.append(f"{count_text(unsaved, 'MP3', 'MP3s')} {verb} been downloaded yet")
    if not parts:
        return None
    return " and ".join(parts) + "."


class RunTracker:
    """Tracks "this run" (items since the last Convert) for the "All done" line.

    ``submitted`` and ``retried`` hide the line; ``update`` returns the new
    line when the queue goes from busy to idle, else None (no change).
    """

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.busy = False

    def submitted(self, item_ids: Iterable[str]) -> None:
        if not self.busy:
            self.ids = set()
        self.ids.update(item_ids)
        self.busy = bool(self.ids)

    def retried(self, item_id: str) -> None:
        self.ids.add(item_id)
        self.busy = True

    def update(self, items: Iterable[ItemSnapshot]) -> str | None:
        items = [item for item in items if item.id in self.ids]
        if not self.busy or any(item.status.is_active for item in items):
            return None
        self.busy = False
        if not items:
            return None
        counts = {status: 0 for status in ItemStatus}
        for item in items:
            counts[item.status] += 1
        return all_done_text(
            counts[ItemStatus.READY],
            counts[ItemStatus.SAVED],
            counts[ItemStatus.FAILED],
            counts[ItemStatus.STOPPED],
        )

"""Thread-based conversion queue (UI-agnostic).

At most ``max_workers`` conversions run at once. Change callbacks are called
from worker threads (and from the caller's thread for add/stop/retry/clear),
never while the internal state lock is held, so a listener may read the
queue from inside a callback. A Qt UI must marshal them to the GUI thread.

Notifications are serialized: a state change and its notification happen
under one "notify" lock, so a listener never receives an older state of an
item after a newer one. Callbacks must therefore return quickly and must not
wait for other threads.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Protocol

from core.converter import (
    Conversion,
    ConversionCallbacks,
    ConversionResult,
    ConverterConfig,
    Phase,
)
from core.errors import FriendlyError, classify_error
from core.saver import discard_staged
from core.urls import ValidLink

MAX_WORKERS = 2


class Runnable(Protocol):
    def run(self) -> ConversionResult: ...

    def stop(self) -> None: ...


# (job_id, url, video_id, callbacks) -> conversion object
ConversionFactory = Callable[[str, str, str, ConversionCallbacks], Runnable]


def factory_for(
    config_provider: Callable[[], ConverterConfig],
    staging_root: Path | None = None,
) -> ConversionFactory:
    """Real conversions. ``config_provider`` is called for every job, so
    settings changes (cookies, tools) apply to the next job.

    With ``staging_root``, each job's finished MP3 goes to its own folder
    ``<staging_root>/<job id>/<title>.mp3`` (the "ready" area) instead of the
    config's output folder, so titles never clash while staged.
    """

    def create(job_id: str, url: str, video_id: str, callbacks: ConversionCallbacks) -> Runnable:
        config = config_provider()
        if staging_root is not None:
            config = replace(config, output_dir=Path(staging_root) / job_id)
        return Conversion(config, url, job_id, callbacks, fallback_name=video_id)

    return create


class ItemStatus(str, Enum):
    """What the user sees: like Phase, but "done" splits into ready/saved."""

    WAITING = "waiting"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    READY = "ready"  # converted, waiting in the staging folder
    SAVED = "saved"  # moved to the user's save folder
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def is_active(self) -> bool:
        return self in (ItemStatus.WAITING, ItemStatus.DOWNLOADING, ItemStatus.CONVERTING)


_PHASE_STATUS = {
    Phase.WAITING: ItemStatus.WAITING,
    Phase.DOWNLOADING: ItemStatus.DOWNLOADING,
    Phase.CONVERTING: ItemStatus.CONVERTING,
    Phase.FAILED: ItemStatus.FAILED,
    Phase.STOPPED: ItemStatus.STOPPED,
}


@dataclass(frozen=True)
class ItemSnapshot:
    id: str
    batch_id: str
    batch_created: float
    url: str
    video_id: str
    title: str
    phase: Phase
    percent: float | None  # only while downloading; None = unknown/not active
    error: FriendlyError | None
    staged_path: Path | None = None  # converted MP3 in the staging folder
    saved_path: Path | None = None  # None until saved
    save_error: FriendlyError | None = None  # last failed save, if any
    staged_missing: bool = False  # the staged file vanished before saving

    @property
    def display_title(self) -> str:
        return self.title or self.url

    @property
    def output_path(self) -> Path | None:
        """Where the converted MP3 is now (saved copy if saved)."""
        return self.saved_path or self.staged_path

    @property
    def status(self) -> ItemStatus:
        if self.phase is Phase.DONE:
            return ItemStatus.SAVED if self.saved_path is not None else ItemStatus.READY
        return _PHASE_STATUS[self.phase]

    @property
    def can_save(self) -> bool:
        return self.status is ItemStatus.READY and not self.staged_missing

    @property
    def can_retry(self) -> bool:
        return self.phase in (Phase.FAILED, Phase.STOPPED) or (
            self.status is ItemStatus.READY and self.staged_missing
        )


@dataclass
class _Item:
    id: str
    batch_id: str
    batch_created: float
    batch_seq: int
    position: int
    url: str
    video_id: str
    title: str = ""
    phase: Phase = Phase.WAITING
    percent: float | None = None
    error: FriendlyError | None = None
    staged_path: Path | None = None
    saved_path: Path | None = None
    save_error: FriendlyError | None = None
    staged_missing: bool = False
    attempt: int = 0
    conversion: Runnable | None = None

    def snapshot(self) -> ItemSnapshot:
        return ItemSnapshot(
            id=self.id,
            batch_id=self.batch_id,
            batch_created=self.batch_created,
            url=self.url,
            video_id=self.video_id,
            title=self.title,
            phase=self.phase,
            percent=self.percent,
            error=self.error,
            staged_path=self.staged_path,
            saved_path=self.saved_path,
            save_error=self.save_error,
            staged_missing=self.staged_missing,
        )

    def reset_result(self) -> None:
        self.percent = None
        self.error = None
        self.staged_path = None
        self.saved_path = None
        self.save_error = None
        self.staged_missing = False
        self.conversion = None


class JobQueue:
    def __init__(
        self,
        factory: ConversionFactory,
        max_workers: int = MAX_WORKERS,
        on_item_changed: Callable[[ItemSnapshot], None] | None = None,
        on_counts_changed: Callable[[], None] | None = None,
        on_items_removed: Callable[[list[str]], None] | None = None,
        clock: Callable[[], float] = time.time,
        staging_root: Path | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._factory = factory
        # MP3s finishing after Stop/Clear are deleted only inside this folder.
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._max_workers = max_workers
        self._on_item_changed = on_item_changed
        self._on_counts_changed = on_counts_changed
        self._on_items_removed = on_items_removed
        self._clock = clock
        self._notify_lock = threading.RLock()  # always taken before _lock
        self._lock = threading.RLock()
        self._items: dict[str, _Item] = {}
        self._pending: deque[str] = deque()
        self._threads: dict[str, threading.Thread] = {}  # key: "<item id>#<attempt>"
        self._killers: set[threading.Thread] = set()  # background stop() calls
        self._batch_seq = itertools.count()
        self._closed = False

    # --- queries ---------------------------------------------------------

    def snapshot(self) -> list[ItemSnapshot]:
        """All items, newest batch first, input order inside a batch."""
        with self._lock:
            items = sorted(
                self._items.values(), key=lambda item: (-item.batch_seq, item.position)
            )
            return [item.snapshot() for item in items]

    def get(self, item_id: str) -> ItemSnapshot | None:
        with self._lock:
            item = self._items.get(item_id)
            return item.snapshot() if item else None

    def counts(self) -> dict[Phase, int]:
        with self._lock:
            counts = {phase: 0 for phase in Phase}
            for item in self._items.values():
                counts[item.phase] += 1
            return counts

    def status_counts(self) -> dict[ItemStatus, int]:
        """Like counts(), but "done" is split into ready (not saved) and saved."""
        with self._lock:
            counts = {status: 0 for status in ItemStatus}
            for item in self._items.values():
                counts[item.snapshot().status] += 1
            return counts

    def unsaved(self) -> list[ItemSnapshot]:
        """Ready items whose MP3 still waits in the staging folder."""
        with self._lock:
            return [
                snapshot
                for snapshot in (item.snapshot() for item in self._items.values())
                if snapshot.can_save
            ]

    @property
    def running_count(self) -> int:
        """Worker threads currently running (a stopped job keeps its slot
        until its process has exited)."""
        with self._lock:
            return len(self._threads)

    def has_active(self) -> bool:
        with self._lock:
            return any(item.phase.is_active for item in self._items.values())

    # --- commands --------------------------------------------------------

    def add(self, links: Iterable[ValidLink]) -> list[str]:
        """Queue links as one new batch. Returns the new item IDs."""
        links = list(links)
        if not links:
            return []
        batch_id = uuid.uuid4().hex
        created = self._clock()
        with self._notify_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("The queue has been shut down")
                seq = next(self._batch_seq)
                new_items = [
                    _Item(
                        id=uuid.uuid4().hex,
                        batch_id=batch_id,
                        batch_created=created,
                        batch_seq=seq,
                        position=position,
                        url=link.url,
                        video_id=link.video_id,
                    )
                    for position, link in enumerate(links)
                ]
                for item in new_items:
                    self._items[item.id] = item
                    self._pending.append(item.id)
                snapshots = [item.snapshot() for item in new_items]
            self._notify(snapshots, counts=True)
        self._pump()
        return [item.id for item in new_items]

    def stop(self, item_id: str) -> bool:
        """Stop a waiting or running item. It shows as "stopped" at once."""
        with self._notify_lock:
            with self._lock:
                item = self._items.get(item_id)
                if item is None or not item.phase.is_active:
                    return False
                conversion = item.conversion
                self._mark_stopped(item)
                snapshot = item.snapshot()
            self._notify([snapshot], counts=True)
        if conversion is not None:
            self._stop_in_background([conversion])
        return True

    def stop_all(self) -> int:
        """Stop every waiting and running item. Returns how many were stopped.

        All items are marked stopped in one step before any process is killed,
        so a freed slot can never start a waiting item in between.
        """
        with self._notify_lock:
            with self._lock:
                active = [item for item in self._items.values() if item.phase.is_active]
                conversions = [item.conversion for item in active if item.conversion]
                for item in active:
                    self._mark_stopped(item)
                snapshots = [item.snapshot() for item in active]
            if snapshots:
                self._notify(snapshots, counts=True)
        if conversions:
            self._stop_in_background(conversions)
        return len(active)

    def retry(self, item_id: str) -> bool:
        """Queue an item again (at the end of the line; the UI row keeps its
        place). Allowed for failed and stopped items, and for ready items
        whose staged file has gone missing."""
        with self._notify_lock:
            with self._lock:
                item = self._items.get(item_id)
                if item is None or self._closed or not item.snapshot().can_retry:
                    return False
                item.attempt += 1
                item.phase = Phase.WAITING
                item.reset_result()
                self._pending.append(item.id)
                snapshot = item.snapshot()
            self._notify([snapshot], counts=True)
        self._pump()
        return True

    def mark_saved(self, item_id: str, saved_path: Path) -> bool:
        """Record that a ready item's MP3 was saved to ``saved_path``."""
        return self._update_done_item(
            item_id, saved_path=Path(saved_path), save_error=None, staged_missing=False
        )

    def mark_save_failed(
        self, item_id: str, error: FriendlyError, staged_missing: bool = False
    ) -> bool:
        """Record a failed save. The item stays ready (unless the staged file
        is gone, which offers Retry instead)."""
        return self._update_done_item(item_id, save_error=error, staged_missing=staged_missing)

    def clear_finished(self, keep: Iterable[str] = ()) -> list[str]:
        """Remove ready, saved, failed and stopped items (except IDs in
        ``keep``). Returns the removed IDs. Files are never touched here."""
        keep = set(keep)
        with self._notify_lock:
            with self._lock:
                removed = [
                    item.id
                    for item in self._items.values()
                    if item.phase.is_finished and item.id not in keep
                ]
                for item_id in removed:
                    del self._items[item_id]
            if removed:
                if self._on_items_removed:
                    self._on_items_removed(removed)
                if self._on_counts_changed:
                    self._on_counts_changed()
        return removed

    def shutdown(self, wait: bool = False, timeout: float | None = None) -> bool:
        """Stop everything and refuse new work.

        Returns at once by default: processes are killed on background
        threads. With ``wait=True`` it blocks until every worker and kill
        thread has finished (or ``timeout`` passes) and returns ``join()``.
        """
        with self._lock:
            self._closed = True
        self.stop_all()
        if not wait:
            return self.is_idle()
        return self.join(timeout)

    def is_idle(self) -> bool:
        """True when no worker or kill thread is running (temp folders are
        removed by the worker before it ends)."""
        with self._lock:
            return not self._threads and not self._killers

    def join(self, timeout: float | None = None) -> bool:
        """Wait for all worker and kill threads. True if they all finished."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._threads.values()) + list(self._killers)
            if not threads:
                return True
            for thread in threads:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                return self.is_idle()

    # --- internals -------------------------------------------------------

    def _stop_in_background(self, conversions: list[Runnable]) -> None:
        """Kill processes on a helper thread so the caller (the GUI) never
        waits for taskkill. The worker thread still removes the temp folder."""

        def kill() -> None:
            try:
                for conversion in conversions:
                    try:
                        conversion.stop()
                    except Exception:  # noqa: BLE001 - one failure must not skip the rest
                        pass
            finally:
                with self._lock:
                    self._killers.discard(threading.current_thread())

        thread = threading.Thread(target=kill, name="stop-conversions", daemon=True)
        with self._lock:
            self._killers.add(thread)
        thread.start()

    def _mark_stopped(self, item: _Item) -> None:
        item.phase = Phase.STOPPED
        item.percent = None
        item.conversion = None
        try:
            self._pending.remove(item.id)
        except ValueError:
            pass

    def _notify(self, snapshots: list[ItemSnapshot], counts: bool) -> None:
        if self._on_item_changed:
            for snapshot in snapshots:
                self._on_item_changed(snapshot)
        if counts and self._on_counts_changed:
            self._on_counts_changed()

    def _pump(self) -> None:
        """Start waiting items while there is a free slot."""
        with self._lock:
            while not self._closed and self._pending and len(self._threads) < self._max_workers:
                item = self._items.get(self._pending.popleft())
                if item is None or item.phase is not Phase.WAITING:
                    continue
                attempt = item.attempt
                callbacks = self._callbacks_for(item.id, attempt)
                conversion = self._factory(f"{item.id}-{attempt}", item.url, item.video_id, callbacks)
                item.conversion = conversion
                key = f"{item.id}#{attempt}"
                thread = threading.Thread(
                    target=self._work,
                    args=(item.id, attempt, key, conversion),
                    name=f"convert-{item.id[:8]}",
                    daemon=True,
                )
                self._threads[key] = thread
                thread.start()

    def _callbacks_for(self, item_id: str, attempt: int) -> ConversionCallbacks:
        def update(**changes) -> None:
            with self._notify_lock:
                with self._lock:
                    item = self._items.get(item_id)
                    if item is None or item.attempt != attempt or not item.phase.is_active:
                        return  # stale callback from a stopped, retried or removed run
                    phase_changed = "phase" in changes and changes["phase"] != item.phase
                    for key, value in changes.items():
                        setattr(item, key, value)
                    snapshot = item.snapshot()
                self._notify([snapshot], counts=phase_changed)

        def on_phase(phase: Phase) -> None:
            if phase.is_active:  # final phases are applied from the result
                update(phase=phase)

        return ConversionCallbacks(
            on_title=lambda title: update(title=title),
            on_progress=lambda percent: update(percent=percent),
            on_phase=on_phase,
        )

    def _work(self, item_id: str, attempt: int, key: str, conversion: Runnable) -> None:
        try:
            try:
                result = conversion.run()
            except Exception as exc:  # defensive: a bug must not kill the queue
                result = ConversionResult(phase=Phase.FAILED, error=classify_error(str(exc)))
            orphan: Path | None = None
            with self._notify_lock:
                snapshot = None
                with self._lock:
                    item = self._items.get(item_id)
                    if item is not None and item.attempt == attempt and item.phase.is_active:
                        item.phase = result.phase
                        item.percent = None
                        item.error = result.error if result.phase is Phase.FAILED else None
                        item.staged_path = result.output_path
                        if result.title:
                            item.title = result.title
                        item.conversion = None
                        snapshot = item.snapshot()
                    elif result.output_path is not None:
                        # Finished just after Stop/Clear: nobody will save it.
                        orphan = result.output_path
                if snapshot is not None:
                    self._notify([snapshot], counts=True)
            if orphan is not None and self._is_staged(orphan):
                discard_staged(orphan)
        finally:
            with self._lock:
                self._threads.pop(key, None)
            self._pump()

    def _is_staged(self, path: Path) -> bool:
        """Only files inside the staging folder may ever be deleted."""
        if self._staging_root is None:
            return False
        try:
            return Path(path).resolve().is_relative_to(self._staging_root.resolve())
        except (OSError, ValueError):
            return False

    def _update_done_item(self, item_id: str, **changes) -> bool:
        with self._notify_lock:
            with self._lock:
                item = self._items.get(item_id)
                if item is None or item.phase is not Phase.DONE:
                    return False
                for key, value in changes.items():
                    setattr(item, key, value)
                snapshot = item.snapshot()
            self._notify([snapshot], counts=True)
        return True


__all__ = [
    "JobQueue",
    "ItemStatus",
    "ItemSnapshot",
    "ConversionFactory",
    "Runnable",
    "factory_for",
    "MAX_WORKERS",
]

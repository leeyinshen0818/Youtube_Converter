"""Save staged MP3s to the user's folder on a worker thread.

Results come back as Qt signals, delivered on the GUI thread.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QObject, Signal

from core import saver


class SaveController(QObject):
    itemSaved = Signal(str, object)  # item id, saved Path
    itemFailed = Signal(str, object, bool)  # item id, FriendlyError, staged file missing
    batchFinished = Signal(int, int, bool)  # saved, failed, was "Download All"

    def __init__(
        self,
        parent: QObject | None = None,
        save_item: Callable[[Path, Path], Path] = saver.save_item,
    ) -> None:
        super().__init__(parent)
        self._save_item = save_item
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()

    def save(self, items: Sequence[tuple[str, Path]], dest_dir: Path, download_all: bool = False) -> None:
        """Save ``(item id, staged path)`` pairs into ``dest_dir`` in the background."""
        items = list(items)
        if not items:
            return

        def work() -> None:
            saved = failed = 0
            try:
                for item_id, staged in items:
                    try:
                        target = self._save_item(staged, dest_dir)
                    except Exception as exc:  # noqa: BLE001 - report every failure on its row
                        failed += 1
                        self.itemFailed.emit(item_id, saver.save_error(exc), isinstance(exc, FileNotFoundError))
                        continue
                    saver.remove_empty_dir(Path(staged).parent)
                    saved += 1
                    self.itemSaved.emit(item_id, target)
                self.batchFinished.emit(saved, failed, download_all)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=work, name="save-mp3", daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()

    def is_idle(self) -> bool:
        with self._lock:
            return not self._threads

    def join(self, timeout: float | None = None) -> bool:
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)
        return self.is_idle()

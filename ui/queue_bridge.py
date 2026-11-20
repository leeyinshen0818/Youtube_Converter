"""Turn JobQueue callbacks (any thread) into Qt signals (GUI thread).

Create the bridge on the GUI thread. Signals emitted from worker threads are
delivered to GUI-thread receivers through Qt's queued connections; emitted
from the GUI thread they are delivered at once. JobQueue serializes its
notifications, so receivers always see an item's states in order.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.jobqueue import ItemSnapshot


class QueueBridge(QObject):
    itemChanged = Signal(object)  # ItemSnapshot
    countsChanged = Signal()
    itemsRemoved = Signal(list)  # list[str] of item ids

    def on_item_changed(self, snapshot: ItemSnapshot) -> None:
        self.itemChanged.emit(snapshot)

    def on_counts_changed(self) -> None:
        self.countsChanged.emit()

    def on_items_removed(self, item_ids: list[str]) -> None:
        self.itemsRemoved.emit(list(item_ids))

    def callbacks(self) -> dict:
        """Keyword arguments for ``JobQueue(...)``."""
        return {
            "on_item_changed": self.on_item_changed,
            "on_counts_changed": self.on_counts_changed,
            "on_items_removed": self.on_items_removed,
        }

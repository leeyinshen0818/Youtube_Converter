"""Flat list model of queue items, in the order they were added.

Only targeted changes: rows are inserted at the end, updated with
``dataChanged`` for that one row, and removed one by one. Never reset.
A retried item keeps its row.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPersistentModelIndex, Qt, Signal

from core.jobqueue import ItemSnapshot, ItemStatus
from ui.theme import BADGES

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1  # -> ItemSnapshot
SAVING_ROLE = Qt.ItemDataRole.UserRole + 2  # -> bool: a save is in progress


def tooltip_for(item: ItemSnapshot) -> str:
    lines = [item.title] if item.title else []
    lines.append(item.url)
    status = item.status
    error = item.save_error if status in (ItemStatus.READY, ItemStatus.SAVED) else item.error
    if error is not None and status in (ItemStatus.FAILED, ItemStatus.READY):
        lines += ["", error.message, error.hint]
        if error.details:
            lines += ["", "Details:", error.details]
    elif status is ItemStatus.SAVED and item.saved_path is not None:
        lines += ["", f"Saved as {item.saved_path}"]
    elif status is ItemStatus.READY and item.staged_path is not None:
        lines += ["", f"Ready: {item.staged_path.name}"]
    return "\n".join(lines)


class QueueModel(QAbstractListModel):
    rowAdded = Signal(str, int)  # item id, row

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[ItemSnapshot] = []
        self._rows: dict[str, int] = {}
        self._saving: set[str] = set()

    # --- Qt model API ------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == ITEM_ROLE:
            return item
        if role == SAVING_ROLE:
            return item.id in self._saving
        if role == Qt.ItemDataRole.DisplayRole:
            return item.display_title
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip_for(item)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{item.display_title}, {BADGES[item.status].label}"
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled

    # --- updates from the queue --------------------------------------------

    def apply(self, item: ItemSnapshot) -> None:
        row = self._rows.get(item.id)
        if row is None:
            row = len(self._items)
            self.beginInsertRows(QModelIndex(), row, row)
            self._items.append(item)
            self._rows[item.id] = row
            self.endInsertRows()
            self.rowAdded.emit(item.id, row)
            return
        self._items[row] = item
        self._changed(row)

    def remove(self, item_ids: Iterable[str]) -> None:
        for item_id in item_ids:
            row = self._rows.get(item_id)
            self._saving.discard(item_id)
            if row is None:
                continue
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._items[row]
            self._reindex(row)
            self.endRemoveRows()

    def set_saving(self, item_id: str, saving: bool) -> None:
        if (item_id in self._saving) == saving:
            return
        (self._saving.add if saving else self._saving.discard)(item_id)
        row = self._rows.get(item_id)
        if row is not None:
            self._changed(row)

    # --- queries -----------------------------------------------------------

    def item(self, row: int) -> ItemSnapshot:
        return self._items[row]

    def items(self) -> list[ItemSnapshot]:
        return list(self._items)

    def is_saving(self, item_id: str) -> bool:
        return item_id in self._saving

    def saving_ids(self) -> set[str]:
        return set(self._saving)

    def row_of(self, item_id: str) -> int | None:
        return self._rows.get(item_id)

    def rows_in_status(self, status: ItemStatus) -> list[int]:
        return [row for row, item in enumerate(self._items) if item.status is status]

    def _changed(self, row: int) -> None:
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)

    def _reindex(self, start: int) -> None:
        self._rows = {item_id: row for item_id, row in self._rows.items() if row < start}
        for row in range(start, len(self._items)):
            self._rows[self._items[row].id] = row

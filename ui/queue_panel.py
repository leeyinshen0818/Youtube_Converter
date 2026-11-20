"""Right column: "3. Your MP3s" with its header actions and row actions."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QListView,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QWidget,
)

from core.jobqueue import ItemStatus
from ui import theme
from ui.messages import summary_text
from ui.queue_delegate import QueueDelegate, action_for
from ui.queue_model import ITEM_ROLE, QueueModel
from ui.theme import FONT, SPACE
from ui.widgets import Card, FlowLayout, WrapLabel

EMPTY_TEXT = "Paste links on the left, then press Convert."
BUSY_INTERVAL_MS = 40
BUSY_STEP = 0.02


class QueueView(QListView):
    """List of rows whose painted buttons react to mouse and keyboard.

    Arrow keys move between rows; Space or Enter presses the row's button.
    """

    actionTriggered = Signal(str, str)  # item id, RowAction value

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.delegate = QueueDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName("Your MP3s")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # --- hit testing -------------------------------------------------------

    def button_hit(self, pos: QPoint) -> int | None:
        """Row whose (enabled) button is under ``pos`` (viewport coordinates)."""
        index = self.indexAt(pos)
        if not index.isValid():
            return None
        item = index.data(ITEM_ROLE)
        if item is None:
            return None
        if not self.delegate.button_at(self.visualRect(index), item, self.font(), pos):
            return None
        return index.row()

    def trigger_row(self, row: int) -> bool:
        model = self.model()
        if model is None or not 0 <= row < model.rowCount():
            return False
        item = model.item(row)
        spec = action_for(item, model.is_saving(item.id))
        if not spec.enabled:
            return False
        self.actionTriggered.emit(item.id, spec.action.value)
        return True

    # --- mouse -------------------------------------------------------------

    def _set_hover(self, row: int | None, on_button: bool) -> None:
        delegate = self.delegate
        if (delegate.hover_row, delegate.hover_button) == (row, on_button):
            return
        old = delegate.hover_row
        delegate.hover_row, delegate.hover_button = row, on_button
        for changed in {old, row} - {None}:
            self.update(self.model().index(changed, 0))
        if on_button:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().unsetCursor()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        button_row = self.button_hit(pos)
        self._set_hover(index.row() if index.isValid() else None, button_row is not None)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        row = self.button_hit(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and row is not None:
            self.delegate.pressed_row = row
            self.setCurrentIndex(self.model().index(row, 0))
            self.update(self.model().index(row, 0))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pressed = self.delegate.pressed_row
        if pressed is not None and event.button() == Qt.MouseButton.LeftButton:
            self.delegate.pressed_row = None
            if self.button_hit(event.position().toPoint()) == pressed:
                self.trigger_row(pressed)
            if pressed < self.model().rowCount():
                self.update(self.model().index(pressed, 0))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self._set_hover(None, False)
        super().leaveEvent(event)

    # --- keyboard ----------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            current = self.currentIndex()
            if current.isValid():
                self.trigger_row(current.row())
                event.accept()
                return
        super().keyPressEvent(event)

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        super().focusInEvent(event)
        model = self.model()
        if model is not None and model.rowCount() and not self.currentIndex().isValid():
            self.setCurrentIndex(model.index(0, 0))


class QueuePanel(Card):
    actionTriggered = Signal(str, str)  # item id, RowAction value
    downloadAllRequested = Signal()
    openFolderRequested = Signal()
    clearFinishedRequested = Signal()

    def __init__(self, model: QueueModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("queuePanel")
        self.model = model

        # Header line 1: title and summary (wraps when narrow).
        head = QWidget()
        head_flow = FlowLayout(head, spacing=SPACE.md)
        self.title = QLabel("3. Your MP3s")
        self.title.setFont(theme.font(FONT.title, bold=True))
        theme.set_role(self.title, "title")
        self.summary = QLabel()
        theme.set_role(self.summary, "hint")
        self.summary.setObjectName("summary")
        head_flow.addWidget(self.title)
        head_flow.addWidget(self.summary)
        self.layout_.addWidget(head)

        # Header line 2: actions (wraps when narrow).
        tools = QWidget()
        tools_flow = FlowLayout(tools, spacing=SPACE.sm)
        self.download_all_button = QPushButton("Download All")
        theme.set_kind(self.download_all_button, "primary")
        self.download_all_button.setToolTip("Save every ready MP3 to the folder")
        self.open_folder_button = QPushButton("Open Folder")
        self.open_folder_button.setToolTip("Open the folder where MP3s are saved")
        self.clear_button = QPushButton("Clear Finished")
        self.clear_button.setToolTip("Remove finished rows from the list (saved MP3s stay)")
        for button in (self.download_all_button, self.open_folder_button, self.clear_button):
            button.setMinimumHeight(theme.control_height(button))
            button.setMinimumWidth(theme.text_width(button, button.text()) + 2 * SPACE.lg + SPACE.sm)
            button.setAutoDefault(False)
            tools_flow.addWidget(button)
        self.layout_.addWidget(tools)

        self.done_line = WrapLabel()
        self.done_line.setObjectName("doneLine")
        self.done_line.hide()
        self.layout_.addWidget(self.done_line)

        # The list, or the empty state.
        self.view = QueueView()
        self.view.setModel(model)
        self.delegate = self.view.delegate
        self.empty = WrapLabel(EMPTY_TEXT)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.set_role(self.empty, "empty")
        self.empty.setContentsMargins(SPACE.lg, SPACE.lg, SPACE.lg, SPACE.lg)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.view)
        self.stack.setMinimumHeight(theme.line_height(self, 4))
        self.layout_.addWidget(self.stack, 1)

        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(BUSY_INTERVAL_MS)
        self._busy_timer.timeout.connect(self._advance_busy)
        self._tall_rows: set[str] = set()
        self._pending_reveal: str | None = None

        self.view.actionTriggered.connect(self.actionTriggered)
        self.download_all_button.clicked.connect(self.downloadAllRequested)
        self.open_folder_button.clicked.connect(self.openFolderRequested)
        self.clear_button.clicked.connect(self.clearFinishedRequested)
        model.rowsInserted.connect(self._on_rows_changed)
        model.rowsRemoved.connect(self._on_rows_changed)
        model.rowAdded.connect(self._on_row_added)
        model.dataChanged.connect(self._on_data_changed)
        self._on_rows_changed()

    # --- public ------------------------------------------------------------

    def reveal(self, item_id: str) -> None:
        """Scroll so the given item is the first visible row (now or once added)."""
        if self.model.row_of(item_id) is None:
            self._pending_reveal = item_id
            return
        self._pending_reveal = None
        QTimer.singleShot(0, lambda: self._scroll_to(item_id))

    def update_header(self) -> None:
        counts = {status: 0 for status in ItemStatus}
        for item in self.model.items():
            counts[item.status] += 1
        text = summary_text(counts)
        self.summary.setText(text)
        self.summary.setVisible(bool(text))
        ready = sum(1 for item in self.model.items() if item.can_save)
        self.download_all_button.setVisible(ready > 0)
        finished = sum(1 for item in self.model.items() if not item.status.is_active)
        self.clear_button.setEnabled(finished > 0)

    def set_done_message(self, text: str | None, role: str = "done") -> None:
        self.done_line.setText(text or "")
        theme.set_role(self.done_line, role)
        self.done_line.setVisible(bool(text))

    def set_save_location(self, label: str) -> None:
        self.delegate.save_location = label
        self.view.viewport().update()

    @property
    def busy_animation_running(self) -> bool:
        return self._busy_timer.isActive()

    # --- internals ---------------------------------------------------------

    def _scroll_to(self, item_id: str) -> None:
        row = self.model.row_of(item_id)
        if row is not None:
            self.view.scrollTo(self.model.index(row), QAbstractItemView.ScrollHint.PositionAtTop)

    def _on_rows_changed(self, *_args) -> None:
        self.stack.setCurrentWidget(self.view if self.model.rowCount() else self.empty)
        self.update_header()

    def _on_row_added(self, item_id: str, _row: int) -> None:
        if self._pending_reveal == item_id:
            self.reveal(item_id)
        self._sync_busy_timer()

    def _on_data_changed(self, top_left: QModelIndex, _bottom_right: QModelIndex, *_roles) -> None:
        item = top_left.data(ITEM_ROLE)
        if item is not None:
            tall = self.delegate.is_tall(item)
            if tall != (item.id in self._tall_rows):
                (self._tall_rows.add if tall else self._tall_rows.discard)(item.id)
                self.delegate.sizeHintChanged.emit(top_left)
        self._sync_busy_timer()
        self.update_header()

    def _sync_busy_timer(self) -> None:
        converting = bool(self.model.rows_in_status(ItemStatus.CONVERTING))
        if converting and not self._busy_timer.isActive():
            self._busy_timer.start()
        elif not converting and self._busy_timer.isActive():
            self._busy_timer.stop()

    def _advance_busy(self) -> None:
        rows = self.model.rows_in_status(ItemStatus.CONVERTING)
        if not rows:
            self._busy_timer.stop()
            return
        self.delegate.busy_position = (self.delegate.busy_position + BUSY_STEP) % 1.0
        for row in rows:  # repaint only the converting rows
            self.view.update(self.model.index(row))

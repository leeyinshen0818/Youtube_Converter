"""'Downloads not working? Update': update yt-dlp on a worker thread.

Closing the dialog while an update runs cancels it; the dialog stays open
(showing "Cancelling...") until the worker has cleaned up.
"""

from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import updater
from core.updater import Stage, UpdateKind, UpdateRequest, UpdateResult
from ui import theme
from ui.dialog_texts import (
    BLOCKED_TEXT,
    CANCELLING_TEXT,
    UPDATE_INTRO,
    current_version_text,
    progress_state,
    result_text,
    stage_text,
)
from ui.services import AppServices, VersionCache
from ui.theme import SPACE
from ui.widgets import WrapLabel, fit_to_content, fitted_size_hint

MIN_WIDTH_CHARS = 52
BLOCKED_POLL_MS = 500  # re-check while downloads run (the window is blocked)


class UpdateRunner(QObject):
    """Runs one update on a thread; signals arrive on the GUI thread."""

    progressed = Signal(object, int, object)  # Stage, done bytes, total bytes or None
    finished = Signal(object)  # UpdateResult

    def __init__(self, services: AppServices, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        services = self.services
        try:
            current = services.find_ytdlp()
        except OSError:
            current = None
        request = UpdateRequest(
            tools_dir=services.tools_dir,
            current_exe=current,
            opener=services.make_opener(),
            command_for=services.command_for,
            progress=self._emit_progress,
            cancel=self.cancel,
            replace=services.replace,
            version_timeout=services.version_timeout,
        )

        def work() -> None:
            result = updater.update_ytdlp(request)
            self._emit(self.finished, result)

        self.thread = threading.Thread(target=work, name="ytdlp-update", daemon=True)
        self.thread.start()

    def _emit_progress(self, stage: Stage, done: int, total: int | None) -> None:
        self._emit(self.progressed, stage, done, total)

    @staticmethod
    def _emit(signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:  # receiver already deleted
            pass

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class UpdateDialog(QDialog):
    def __init__(
        self,
        services: AppServices,
        versions: VersionCache,
        is_busy: Callable[[], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.services = services
        self.versions = versions
        self.is_busy = is_busy
        self.runner: UpdateRunner | None = None
        self.result: UpdateResult | None = None
        self._close_when_done = False
        self.setWindowTitle("Update")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE.lg, SPACE.lg, SPACE.lg, SPACE.lg)
        layout.setSpacing(SPACE.md)
        intro = WrapLabel(UPDATE_INTRO)
        layout.addWidget(intro)
        self.current_label = QLabel(current_version_text(versions.value))
        self.current_label.setObjectName("currentVersion")
        theme.set_role(self.current_label, "hint")
        layout.addWidget(self.current_label)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMinimumHeight(theme.line_height(self) // 2)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.message = WrapLabel()
        self.message.setObjectName("updateMessage")
        self.message.hide()
        layout.addWidget(self.message)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE.sm)
        self.update_button = QPushButton("Check and update")
        theme.set_kind(self.update_button, "primary")
        self.cancel_button = QPushButton("Cancel")
        self.close_button = QPushButton("Close")
        for button in (self.update_button, self.cancel_button, self.close_button):
            button.setMinimumHeight(theme.control_height(button))
        self.cancel_button.hide()
        buttons.addWidget(self.update_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.setMinimumWidth(theme.average_chars(self, MIN_WIDTH_CHARS))

        self.update_button.clicked.connect(self.start_update)
        self.cancel_button.clicked.connect(self.cancel_update)
        self.close_button.clicked.connect(self.close)
        versions.changed.connect(self._on_version)
        self._blocked_timer = QTimer(self)
        self._blocked_timer.setInterval(BLOCKED_POLL_MS)
        self._blocked_timer.timeout.connect(self.refresh_blocked)
        versions.request()
        self.refresh_blocked()
        fit_to_content(self)

    def sizeHint(self) -> QSize:  # noqa: N802
        return fitted_size_hint(self, super().sizeHint())

    # --- state ----------------------------------------------------------------

    def is_running(self) -> bool:
        return self.runner is not None and self.runner.is_running()

    def _set_message(self, text: str, role: str = "message", tooltip: str = "") -> None:
        self.message.setText(text)
        self.message.setToolTip(tooltip)
        theme.set_role(self.message, role)
        self.message.setVisible(bool(text))
        fit_to_content(self)

    def refresh_blocked(self) -> bool:
        """Show the "please wait" note while downloads run. Returns blocked."""
        if self.is_running():
            return False
        blocked = self.is_busy()
        self.update_button.setEnabled(not blocked)
        if blocked:
            self._blocked_timer.start()
        else:
            self._blocked_timer.stop()
        if blocked:
            self._set_message(BLOCKED_TEXT, "warn")
        elif self.message.text() == BLOCKED_TEXT:
            self._set_message("")
        return blocked

    def start_update(self) -> bool:
        if self.is_running() or self.refresh_blocked():
            return False
        self.result = None
        self.update_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.progress.show()
        fit_to_content(self)
        self._on_progress(Stage.CHECKING, 0, None)
        self.runner = UpdateRunner(self.services, self)
        self.runner.progressed.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.runner.start()
        return True

    def cancel_update(self) -> None:
        if not self.is_running() or self.runner is None:
            return
        self.runner.cancel.set()
        self.cancel_button.setEnabled(False)
        self._set_message(CANCELLING_TEXT)

    # --- worker signals ---------------------------------------------------------

    def _on_progress(self, stage: Stage, done: int, total: int | None) -> None:
        if self.runner is not None and self.runner.cancel.is_set():
            return
        state = progress_state(stage, done, total)
        self.progress.setRange(0, state.maximum)
        self.progress.setValue(state.value)
        self._set_message(stage_text(stage))

    def _on_finished(self, result: UpdateResult) -> None:
        if self.runner is not None and self.runner.thread is not None:
            self.runner.thread.join(1.0)
        self.result = result
        self.progress.hide()
        self.cancel_button.hide()
        self.update_button.setEnabled(True)
        fit_to_content(self)
        text = result_text(result)
        self._set_message(text.text, text.role, text.tooltip)
        if result.kind in (UpdateKind.UPDATED, UpdateKind.INSTALLED, UpdateKind.ALREADY_LATEST):
            self.versions.set(result.version)
        if self._close_when_done:
            self.close()

    def _on_version(self, _version: str) -> None:
        # Read the cache: a queued older signal may arrive after a newer value.
        self.current_label.setText(current_version_text(self.versions.value))

    # --- closing ----------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.is_running():
            self._close_when_done = True
            self.cancel_update()
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        # Esc and the title bar's close button both end up here.
        if self.is_running():
            self._close_when_done = True
            self.cancel_update()
            return
        super().reject()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

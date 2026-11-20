"""Main window: left "1. Paste links", right "3. Your MP3s".

Flow: Convert -> MP3s wait in the staging folder ("Ready") -> the user
clicks Download / Download All -> they move to the save folder ("Saved").
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QGuiApplication, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import tools
from core.converter import ConverterConfig, remove_dir
from core.jobqueue import ItemSnapshot, JobQueue, factory_for
from core.saver import default_save_dir, discard_staged
from core.updater import tools_busy
from core.urls import ValidLink
from core.version import APP_VERSION
from ui import theme
from ui.dialog_texts import SETTINGS_BUTTON_TEXT, UPDATE_LINK_TEXT
from ui.input_panel import InputPanel
from ui.messages import (
    RunTracker,
    clear_confirmation_text,
    close_warning_text,
    save_location_label,
    saved_text,
)
from ui.queue_bridge import QueueBridge
from ui.queue_delegate import RowAction
from ui.queue_model import QueueModel
from ui.queue_panel import QueuePanel
from ui.saving import SaveController
from ui.services import AppServices, VersionCache
from ui.settings_dialog import SettingsDialog
from ui.update_dialog import UpdateDialog
from ui.theme import SPACE

WINDOW_TITLE = f"YouTube to MP3 Converter {APP_VERSION}"
CLOSE_TIMEOUT_S = 5.0
CLOSE_POLL_MS = 50
MAXIMIZE_AT_OR_BELOW = 1100  # logical px of available screen width
DEFAULT_SCREEN_SHARE = 0.85
WIDE_LAYOUT_FROM = 1700  # logical px of window width: give the queue more room
COMPACT_HEIGHT_LINES = 30  # below this many text lines of height, compact notice

QueueFactory = Callable[[QueueBridge], JobQueue]


def staging_queue_factory(ready_root: Path | None) -> QueueFactory:
    """Real conversions whose MP3s wait in ``<ready_root>/<job id>/``."""

    def create(bridge: QueueBridge) -> JobQueue:
        def config() -> ConverterConfig:
            return ConverterConfig.from_system(output_dir=ready_root or default_save_dir())

        return JobQueue(
            factory_for(config, staging_root=ready_root),
            staging_root=ready_root,
            **bridge.callbacks(),
        )

    return create


def clean_folder(folder: Path | None) -> None:
    """Delete everything inside ``folder`` (only call when nothing runs)."""
    if folder is None or not folder.is_dir():
        return
    for child in folder.iterdir():
        if child.is_dir():
            remove_dir(child, attempts=3, delay=0.1)
        else:
            try:
                child.unlink()
            except OSError:
                pass



class ShellActions:
    """Explorer integration (replaced by a fake in tests)."""

    def open_folder(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # type: ignore[attr-defined]

    def show_in_folder(self, path: Path) -> None:
        subprocess.Popen(f'explorer /select,"{path}"')


class MainWindow(QMainWindow):
    def __init__(
        self,
        queue_factory: QueueFactory | None = None,
        temp_root: Path | None = None,
        ready_root: Path | None = None,
        save_dir: Path | None = None,
        shell: ShellActions | None = None,
        services: AppServices | None = None,
    ) -> None:
        """``services`` gives access to settings, cookies, yt-dlp and the
        network. Without it nothing outside the window is touched: settings
        stay in memory (starting with ``save_dir``), no yt-dlp is found and
        the network is off."""
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.temp_root = temp_root
        self.ready_root = ready_root
        if services is not None and save_dir is not None:
            raise ValueError("pass save_dir through services.settings")
        self.services = services or AppServices.detached(save_dir)
        self.versions = VersionCache(self.services, self)
        self.shell = shell or ShellActions()
        self.settings_dialog: SettingsDialog | None = None
        self.update_dialog: UpdateDialog | None = None
        clean_folder(temp_root)
        clean_folder(ready_root)

        self.bridge = QueueBridge(self)
        self.model = QueueModel(self)
        # Always queued: updates from worker threads are queued anyway, so a
        # direct delivery from the GUI thread (e.g. mark_saved) could overtake
        # an older queued one and be overwritten by it. Queuing everything
        # keeps the order in which JobQueue emitted them.
        queued = Qt.ConnectionType.QueuedConnection
        self.bridge.itemChanged.connect(self.model.apply, queued)
        self.bridge.itemsRemoved.connect(self.model.remove, queued)
        self.queue = (queue_factory or staging_queue_factory(ready_root))(self.bridge)
        self.saver = SaveController(self)
        self.run = RunTracker()

        central = QWidget()
        central.setObjectName("central")
        outer = QVBoxLayout(central)
        # The slim top bar (Settings) and footer (Update link) sit in the
        # outer margins' space so the columns keep their height.
        outer.setContentsMargins(SPACE.lg, 0, SPACE.lg, 0)
        outer.setSpacing(0)

        self.top_bar = QWidget()
        self.top_bar.setObjectName("topBar")
        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        top.addStretch(1)
        self.settings_button = QPushButton(SETTINGS_BUTTON_TEXT)
        theme.set_kind(self.settings_button, "link")
        self.settings_button.setFont(theme.font(theme.FONT.small))
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        top.addWidget(self.settings_button)
        outer.addWidget(self.top_bar)

        self.body = QHBoxLayout()
        self.body.setSpacing(SPACE.lg)
        self.input_panel = InputPanel()
        self.queue_panel = QueuePanel(self.model)
        self.queue_panel.set_save_location(save_location_label(self.save_dir))
        self.body.addWidget(self.input_panel, 2)
        self.body.addWidget(self.queue_panel, 3)
        outer.addLayout(self.body, 1)

        self.footer = QWidget()
        self.footer.setObjectName("footer")
        bottom = QHBoxLayout(self.footer)
        bottom.setContentsMargins(0, 0, 0, 0)
        self.update_link = QPushButton(UPDATE_LINK_TEXT)
        theme.set_kind(self.update_link, "link")
        self.update_link.setFont(theme.font(theme.FONT.small))
        self.update_link.setCursor(Qt.CursorShape.PointingHandCursor)
        bottom.addWidget(self.update_link)
        bottom.addStretch(1)
        outer.addWidget(self.footer)
        self.setCentralWidget(central)

        panel = self.queue_panel
        order = [
            self.input_panel.input,
            self.input_panel.paste_button,
            self.input_panel.clear_button,
            self.input_panel.convert_button,
            panel.view,
            panel.download_all_button,
            panel.open_folder_button,
            panel.clear_button,
            self.update_link,
            self.settings_button,
        ]
        for first, second in zip(order, order[1:]):
            self.setTabOrder(first, second)

        self.input_panel.linksSubmitted.connect(self.submit_links)
        panel.actionTriggered.connect(self.handle_action)
        panel.downloadAllRequested.connect(self.download_all)
        panel.openFolderRequested.connect(self.open_folder)
        panel.clearFinishedRequested.connect(self.clear_finished)
        self.settings_button.clicked.connect(self.open_settings)
        self.update_link.clicked.connect(self.open_update)
        self.bridge.countsChanged.connect(self._on_counts_changed, Qt.ConnectionType.QueuedConnection)
        self.saver.itemSaved.connect(self._on_item_saved)
        self.saver.itemFailed.connect(self._on_item_save_failed)
        self.saver.batchFinished.connect(self._on_batch_saved)

        self._focused_once = False
        self._closing = False
        self._close_finished = False
        self._close_deadline = 0.0
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(CLOSE_POLL_MS)
        self._close_timer.timeout.connect(self._poll_close)

    @property
    def save_dir(self) -> Path:
        """The user's save folder (Settings), read fresh on every use."""
        return self.services.settings.save_dir

    # --- dialogs (replaceable in tests) -------------------------------------

    def confirm(self, text: str, title: str, accept_label: str, reject_label: str) -> bool:
        """Ask a yes/no question; the safe (reject) button is the default."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        accept = box.addButton(accept_label, QMessageBox.ButtonRole.DestructiveRole)
        reject = box.addButton(reject_label, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(reject)
        box.setEscapeButton(reject)
        box.exec()
        return box.clickedButton() is accept

    # --- settings and update ------------------------------------------------

    def open_settings(self) -> SettingsDialog:
        """Show Settings (modal, non-blocking: ``open()`` not ``exec()``)."""
        dialog = SettingsDialog(self.services, self.versions, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.saveDirChanged.connect(self._on_save_dir_changed)
        dialog.destroyed.connect(self._forget_settings_dialog)
        self.settings_dialog = dialog
        dialog.open()
        return dialog

    def open_update(self) -> UpdateDialog:
        dialog = UpdateDialog(self.services, self.versions, self.downloads_running, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.destroyed.connect(self._forget_update_dialog)
        self.update_dialog = dialog
        dialog.open()
        return dialog

    def downloads_running(self) -> bool:
        """yt-dlp may be running (its exe is locked on Windows)."""
        return tools_busy(self.queue.has_active(), self.queue.running_count)

    def _forget_settings_dialog(self) -> None:
        self.settings_dialog = None

    def _forget_update_dialog(self) -> None:
        self.update_dialog = None

    def _on_save_dir_changed(self, folder: Path) -> None:
        self.queue_panel.set_save_location(save_location_label(folder))

    # --- queue -------------------------------------------------------------

    def submit_links(self, links: Sequence[ValidLink]) -> list[str]:
        ids = self.queue.add(links)
        if ids:
            self.run.submitted(ids)
            self.queue_panel.set_done_message(None)
            self.queue_panel.reveal(ids[0])
        return ids

    def handle_action(self, item_id: str, action: str) -> None:
        item = self.queue.get(item_id)
        if item is None:
            return
        if action == RowAction.STOP.value:
            self.queue.stop(item_id)
        elif action == RowAction.RETRY.value:
            if self.queue.retry(item_id):
                self.run.retried(item_id)
                self.queue_panel.set_done_message(None)
        elif action == RowAction.DOWNLOAD.value:
            self.save_items([item])
        elif action == RowAction.SHOW.value:
            if item.saved_path is not None and item.saved_path.exists():
                self.shell.show_in_folder(item.saved_path)
            else:
                self.shell.open_folder(self.save_dir)

    # --- saving ------------------------------------------------------------

    def save_items(self, items: Sequence[ItemSnapshot], download_all: bool = False) -> int:
        pairs = []
        for item in items:
            if not item.can_save or self.model.is_saving(item.id) or item.staged_path is None:
                continue
            self.model.set_saving(item.id, True)
            pairs.append((item.id, item.staged_path))
        self.saver.save(pairs, self.save_dir, download_all=download_all)
        return len(pairs)

    def download_all(self) -> int:
        return self.save_items(self.queue.unsaved(), download_all=True)

    def open_folder(self) -> None:
        self.shell.open_folder(self.save_dir)

    def _on_item_saved(self, item_id: str, path: Path) -> None:
        self.model.set_saving(item_id, False)
        self.queue.mark_saved(item_id, path)

    def _on_item_save_failed(self, item_id: str, error, staged_missing: bool) -> None:
        self.model.set_saving(item_id, False)
        self.queue.mark_save_failed(item_id, error, staged_missing=staged_missing)

    def _on_batch_saved(self, saved: int, failed: int, download_all: bool) -> None:
        if download_all and saved + failed:
            text = saved_text(saved, failed, save_location_label(self.save_dir))
            self.queue_panel.set_done_message(text, "warn" if failed else "done")

    def _on_counts_changed(self) -> None:
        message = self.run.update(self.model.items())
        if message:
            self.queue_panel.set_done_message(message, "done")

    # --- clear finished ----------------------------------------------------

    def clear_finished(self) -> list[str]:
        saving = self.model.saving_ids()
        unsaved = [item for item in self.queue.unsaved() if item.id not in saving]
        question = clear_confirmation_text(len(unsaved))
        if question and not self.confirm(question, "Clear finished", "Clear anyway", "Cancel"):
            return []
        removed = self.queue.clear_finished(keep=saving)
        doomed = [item.staged_path for item in unsaved if item.id in set(removed)]
        if doomed and self.ready_root is not None:
            threading.Thread(
                target=lambda: [discard_staged(path) for path in doomed],
                name="discard-staged",
                daemon=True,
            ).start()
        return removed

    # --- geometry ----------------------------------------------------------

    def show_initial(self) -> None:
        """85 % of the available screen, centered; maximized on small screens."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        if available is None:
            self.show()
            return
        if available.width() <= MAXIMIZE_AT_OR_BELOW:
            self.showMaximized()
            return
        width = int(available.width() * DEFAULT_SCREEN_SHARE)
        height = int(available.height() * DEFAULT_SCREEN_SHARE)
        minimum = self.minimumSizeHint()
        width, height = max(width, minimum.width()), max(height, minimum.height())
        self.resize(min(width, available.width()), min(height, available.height()))
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())
        self.show()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        wide = self.width() >= WIDE_LAYOUT_FROM
        self.body.setStretch(0, 1 if wide else 2)
        self.body.setStretch(1, 2 if wide else 3)
        compact = self.height() < theme.line_height(self, COMPACT_HEIGHT_LINES)
        self.input_panel.set_compact(compact)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._focused_once:
            self._focused_once = True
            QTimer.singleShot(0, self._focus_input)

    def _focus_input(self) -> None:
        self.activateWindow()
        self.input_panel.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    # --- closing -----------------------------------------------------------

    def close_warning(self) -> str | None:
        counts = self.queue.status_counts()
        running = sum(n for status, n in counts.items() if status.is_active)
        return close_warning_text(running, len(self.queue.unsaved()))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._close_finished:
            event.accept()
            return
        if not self._closing:
            warning = self.close_warning()
            if warning and not self.confirm(warning, "Quit?", "Quit", "Go back"):
                event.ignore()
                return
            self._closing = True
            self.queue.shutdown()  # marks everything stopped; kills off-thread
            if self.queue.is_idle() and self.saver.is_idle():
                self._finish_close()
                event.accept()
                return
            self.setEnabled(False)
            self.setWindowTitle(f"{WINDOW_TITLE} - stopping downloads...")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._close_deadline = time.monotonic() + CLOSE_TIMEOUT_S
            self._close_timer.start()
        event.ignore()

    def _poll_close(self) -> None:
        idle = self.queue.is_idle() and self.saver.is_idle()
        if idle or time.monotonic() >= self._close_deadline:
            self._close_timer.stop()
            QApplication.restoreOverrideCursor()
            self._finish_close()
            self.close()

    def _finish_close(self) -> None:
        self.queue.join(0.5)
        self.saver.join(0.5)
        clean_folder(self.temp_root)
        clean_folder(self.ready_root)  # unsaved MP3s are dropped on quit
        self._close_finished = True


def temp_root_for_app() -> Path:
    return tools.temp_root(tools.default_environment())


def ready_root_for_app() -> Path:
    return tools.ready_root(tools.default_environment())

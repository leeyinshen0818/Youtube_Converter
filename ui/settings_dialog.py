"""Settings: save folder, cookies (under "Advanced"), versions. Changes apply
immediately; the only button is Close."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import cookies
from core.settings import check_writable
from ui import theme
from ui.dialog_texts import (
    COOKIES_HELP,
    NOT_WRITABLE_TEXT,
    SETTINGS_NOT_SAVED_TEXT,
    app_version_text,
    component_version_text,
    cookie_status_text,
)
from ui.services import AppServices, VersionCache
from ui.theme import FONT, SPACE
from ui.widgets import ElidedLabel, FlowLayout, WrapLabel, fit_to_content, fitted_size_hint

ADVANCED_CLOSED = "Show advanced"
ADVANCED_OPEN = "Hide advanced"
MIN_WIDTH_CHARS = 60


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setFont(theme.font(FONT.body, bold=True))
    theme.set_role(label, "title")
    return label


def _button(text: str, kind: str | None = None) -> QPushButton:
    button = QPushButton(text)
    if kind:
        theme.set_kind(button, kind)
    button.setMinimumHeight(theme.control_height(button))
    return button


def _row(*buttons: QPushButton) -> QWidget:
    row = QWidget()
    flow = FlowLayout(row)
    for button in buttons:
        flow.addWidget(button)
    return row


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setStyleSheet(f"color: {theme.COLOR.border};")
    return line


class SettingsDialog(QDialog):
    saveDirChanged = Signal(object)  # Path

    def __init__(self, services: AppServices, versions: VersionCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.versions = versions
        self.setWindowTitle("Settings")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE.lg, SPACE.lg, SPACE.lg, SPACE.lg)
        layout.setSpacing(SPACE.sm)

        # --- save folder --------------------------------------------------
        layout.addWidget(_section_title("Save folder"))
        self.folder_label = ElidedLabel()
        self.folder_label.setObjectName("saveFolder")
        self.change_button = _button("Change...")
        self.reset_button = _button("Reset to default")
        folder_row = QHBoxLayout()
        folder_row.setSpacing(SPACE.sm)
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.change_button)
        folder_row.addWidget(self.reset_button)
        layout.addLayout(folder_row)
        self.folder_message = WrapLabel()
        theme.set_role(self.folder_message, "error")
        self.folder_message.hide()
        layout.addWidget(self.folder_message)

        layout.addWidget(_divider())

        # --- advanced: cookies --------------------------------------------
        self.advanced_button = _button(ADVANCED_CLOSED, "link")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setAccessibleName("Advanced")
        layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced = QWidget()
        self.advanced.setObjectName("advanced")
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(SPACE.sm)
        self.cookie_status = QLabel()
        self.cookie_status.setObjectName("cookieStatus")
        advanced.addWidget(self.cookie_status)
        help_text = WrapLabel(COOKIES_HELP)
        help_text.setFont(theme.font(FONT.small))
        theme.set_role(help_text, "hint")
        advanced.addWidget(help_text)
        self.import_button = _button("Import cookies...")
        self.remove_button = _button("Remove")
        advanced.addWidget(_row(self.import_button, self.remove_button))
        self.cookie_message = WrapLabel()
        self.cookie_message.hide()
        advanced.addWidget(self.cookie_message)
        self.advanced.hide()
        layout.addWidget(self.advanced)

        layout.addWidget(_divider())

        # --- versions -----------------------------------------------------
        self.app_version = QLabel(app_version_text())
        self.component_version = QLabel(component_version_text(versions.value))
        self.component_version.setObjectName("componentVersion")
        for label in (self.app_version, self.component_version):
            theme.set_role(label, "hint")
            layout.addWidget(label)

        layout.addStretch(1)
        buttons = QDialogButtonBox()
        self.close_button = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.close_button.setMinimumHeight(theme.control_height(self.close_button))
        self.close_button.setDefault(True)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setMinimumWidth(theme.average_chars(self, MIN_WIDTH_CHARS))

        self.change_button.clicked.connect(self.change_folder)
        self.reset_button.clicked.connect(self.reset_folder)
        self.advanced_button.toggled.connect(self.set_advanced_open)
        self.import_button.clicked.connect(self.import_cookies)
        self.remove_button.clicked.connect(self.remove_cookies)
        versions.changed.connect(self._on_version)

        self.refresh_folder()
        self.refresh_cookies()
        fit_to_content(self)
        versions.request()

    def sizeHint(self) -> QSize:  # noqa: N802
        return fitted_size_hint(self, super().sizeHint())

    # --- file pickers (replaced in tests) -------------------------------------

    def choose_folder(self) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder for your MP3s", str(self.services.settings.save_dir))
        return Path(chosen) if chosen else None

    def choose_cookies_file(self) -> Path | None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose your cookies.txt", str(Path.home()), "Cookies file (*.txt);;All files (*)"
        )
        return Path(chosen) if chosen else None

    # --- save folder --------------------------------------------------------

    def refresh_folder(self) -> None:
        settings = self.services.settings.settings
        self.folder_label.setText(str(settings.save_dir))
        self.reset_button.setEnabled(not settings.uses_default_save_dir)

    def _show_folder_message(self, text: str | None) -> None:
        self.folder_message.setText(text or "")
        self.folder_message.setVisible(bool(text))
        fit_to_content(self)

    def apply_folder(self, folder: Path | None) -> bool:
        """Use ``folder`` (None = default) if it is writable."""
        store = self.services.settings
        target = Path(folder) if folder else None
        if target is not None and not check_writable(target):
            self._show_folder_message(NOT_WRITABLE_TEXT)
            return False
        message = None
        try:
            store.set_save_dir(target)
        except OSError:
            message = SETTINGS_NOT_SAVED_TEXT
        self._show_folder_message(message)
        self.refresh_folder()
        self.saveDirChanged.emit(store.save_dir)
        return True

    def change_folder(self) -> bool:
        folder = self.choose_folder()
        if folder is None:
            return False
        return self.apply_folder(folder)

    def reset_folder(self) -> bool:
        return self.apply_folder(None)

    # --- cookies ------------------------------------------------------------

    def set_advanced_open(self, open_: bool) -> None:
        if self.advanced_button.isChecked() != open_:
            self.advanced_button.setChecked(open_)  # re-enters through toggled
            return
        self.advanced.setVisible(open_)
        self.advanced_button.setText(ADVANCED_OPEN if open_ else ADVANCED_CLOSED)
        fit_to_content(self)

    def refresh_cookies(self) -> None:
        status = cookies.cookie_status(self.services.app_data, self.services.app_dir)
        self.cookie_status.setText(cookie_status_text(status))
        self.remove_button.setEnabled(status is cookies.CookieStatus.IMPORTED)

    def _show_cookie_result(self, result: cookies.CookieResult) -> None:
        self.cookie_message.setText(result.message)
        theme.set_role(self.cookie_message, "done" if result.ok else "error")
        self.cookie_message.show()
        self.refresh_cookies()
        fit_to_content(self)

    def import_cookies(self) -> bool:
        source = self.choose_cookies_file()
        if source is None:
            return False
        result = cookies.import_cookies(source, self.services.app_data)
        self._show_cookie_result(result)
        return result.ok

    def remove_cookies(self) -> bool:
        result = cookies.remove_cookies(self.services.app_data)
        self._show_cookie_result(result)
        return result.ok

    # --- versions -----------------------------------------------------------

    def _on_version(self, _version: str) -> None:
        # Read the cache: a queued older signal may arrive after a newer value.
        self.component_version.setText(component_version_text(self.versions.value))

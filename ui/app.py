"""Application start-up: DPI policy, Fusion style, theme, icon, main window."""

from __future__ import annotations

import sys
from pathlib import Path

import shiboken6
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.version import APP_VERSION
from ui import theme
from ui.main_window import MainWindow, ready_root_for_app, temp_root_for_app

APP_NAME = "YouTube to MP3 Converter"


def resource_dir() -> Path:
    """Folder holding bundled resources (PyInstaller) or the source tree."""
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) if bundle else Path(__file__).resolve().parent.parent


def icon_path() -> Path:
    return resource_dir() / "icon" / "icon1.png"


def main(argv: list[str] | None = None) -> int:
    theme.configure_high_dpi()  # must run before QApplication exists
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    theme.apply_theme(app)
    icon = icon_path()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    window = MainWindow(temp_root=temp_root_for_app(), ready_root=ready_root_for_app())
    window.show_initial()
    code = app.exec()
    # Delete the window while the QApplication still exists instead of
    # leaving it to Python's shutdown order.
    shiboken6.delete(window)
    return code

"""Application start-up: single instance, DPI policy, Fusion style, theme,
icon, main window."""

from __future__ import annotations

import sys
from pathlib import Path

import shiboken6
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.single_instance import APP_MUTEX_NAME, SingleInstance
from core.version import APP_VERSION
from ui import theme
from ui.main_window import MainWindow, ready_root_for_app, temp_root_for_app
from ui.services import AppServices

APP_NAME = "YouTube to MP3 Converter"
LOGO_FILE = "logo.png"  # source logo in icon\; the build turns it into app.ico


def resource_dir() -> Path:
    """Folder holding bundled resources (PyInstaller) or the source tree."""
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) if bundle else Path(__file__).resolve().parent.parent


def icon_path(base: Path | None = None) -> Path:
    """The multi-size .ico made by the build (sharp at every scale), else the
    source logo (running from source)."""
    folder = (base or resource_dir()) / "icon"
    ico = folder / "app.ico"
    return ico if ico.is_file() else folder / LOGO_FILE


def main(argv: list[str] | None = None, instance_name: str = APP_MUTEX_NAME) -> int:
    # A second copy (e.g. a double double-click) exits quietly before any
    # window or Qt object exists; the guard lives until this process ends.
    instance = SingleInstance(instance_name)
    if not instance.acquire():
        return 0
    try:
        return _run(argv)
    finally:
        instance.release()


def _run(argv: list[str] | None) -> int:
    theme.configure_high_dpi()  # must run before QApplication exists
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    theme.apply_theme(app)
    icon = icon_path()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    window = MainWindow(
        temp_root=temp_root_for_app(),
        ready_root=ready_root_for_app(),
        services=AppServices.for_app(),
    )
    window.show_initial()
    code = app.exec()
    # Delete the window while the QApplication still exists instead of
    # leaving it to Python's shutdown order.
    shiboken6.delete(window)
    return code

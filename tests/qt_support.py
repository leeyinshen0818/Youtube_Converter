"""Shared Qt setup for UI tests (offscreen, set here and never in the app)."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
if _fonts.is_dir():
    os.environ.setdefault("QT_QPA_FONTDIR", str(_fonts))

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402

_app: QApplication | None = None


def qt_app() -> QApplication:
    global _app
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    theme.configure_high_dpi()
    _app = QApplication([])
    theme.apply_theme(_app)
    return _app


def process_events(times: int = 5) -> None:
    app = qt_app()
    for _ in range(times):
        app.processEvents()

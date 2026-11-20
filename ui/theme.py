"""Design tokens and the application theme.

Font sizes are set on widgets in code (see ``font()``), never in the
stylesheet, so font-metrics based minimum sizes always see the real font.

Colors come from the old browser UI. Sizes are logical pixels (Qt scales them
by the screen's device pixel ratio) and font sizes are points, so everything
scales naturally with Windows display scaling. Heights of text-bearing
controls are never fixed here: widgets derive them from font metrics.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from core.jobqueue import ItemStatus


@dataclass(frozen=True)
class Colors:
    window_bg: str = "#f7f9fc"
    card_bg: str = "#ffffff"
    border: str = "#d9dee8"
    row_divider: str = "#edf0f5"
    text: str = "#15171c"
    muted: str = "#5c6370"
    accent: str = "#0e7c66"
    accent_dark: str = "#075f4d"
    accent_focus: str = "#0e7c66"
    secondary_bg: str = "#eef1f6"
    secondary_hover: str = "#dfe5ee"
    secondary_text: str = "#334155"
    danger: str = "#b83232"
    danger_bg: str = "#ffecec"
    warn: str = "#956400"
    done: str = "#19724f"
    notice_bg: str = "#fff8e6"
    notice_border: str = "#ead5a5"
    notice_text: str = "#5f4400"
    track: str = "#e6eaf0"
    disabled_bg: str = "#c9d2dc"
    disabled_text: str = "#f4f6f9"
    selection: str = "#cfe8e2"


@dataclass(frozen=True)
class Spacing:
    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16


@dataclass(frozen=True)
class Radii:
    control: int = 6
    card: int = 10


@dataclass(frozen=True)
class FontSizes:
    """Point sizes."""

    body: float = 11.0
    small: float = 10.0
    title: float = 14.0
    convert: float = 13.0
    input: float = 11.0


COLOR = Colors()
SPACE = Spacing()
RADIUS = Radii()
FONT = FontSizes()
FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


@dataclass(frozen=True)
class BadgeStyle:
    label: str
    background: str
    text: str
    stripe: str
    outlined: bool = False


BADGES: dict[ItemStatus, BadgeStyle] = {
    ItemStatus.WAITING: BadgeStyle("Waiting", "#ffffff", "#445063", COLOR.border, outlined=True),
    ItemStatus.DOWNLOADING: BadgeStyle("Downloading", "#eaf2ff", "#1c5fb8", "#1c5fb8"),
    ItemStatus.CONVERTING: BadgeStyle("Converting", "#e6f4f1", "#0e7c66", "#0e7c66"),
    # Ready uses the old "Done" colors; Saved is a calmer variant of them.
    ItemStatus.READY: BadgeStyle("Ready", "#e8f6ef", "#19724f", "#19724f"),
    ItemStatus.SAVED: BadgeStyle("Saved", "#f2f7f4", "#4f7d66", "#a9cdb9"),
    ItemStatus.FAILED: BadgeStyle("Failed", "#ffecec", "#b83232", "#b83232"),
    ItemStatus.STOPPED: BadgeStyle("Stopped", "#eef1f6", "#445063", "#718096"),
}

# Dynamic properties read by the stylesheet.
ROLE = "role"  # QLabel: title | hint | message | error | warn | empty
KIND = "kind"  # QPushButton: primary | convert ; QFrame: card | notice


def configure_high_dpi() -> None:
    """Keep fractional scale factors (125 %, 150 %) exact.

    Must run before the QApplication is created.
    """
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


# --- font metrics helpers ------------------------------------------------------


def font(point_size: float = FONT.body, *, bold: bool = False, family: str = FONT_FAMILY) -> QFont:
    result = QFont(family)
    result.setPointSizeF(point_size)
    if bold:
        result.setWeight(QFont.Weight.Bold)
    return result


def line_height(target: QWidget | QFont, lines: int = 1) -> int:
    metrics = QFontMetrics(target if isinstance(target, QFont) else target.font())
    return metrics.lineSpacing() * (lines - 1) + metrics.height()


def text_width(target: QWidget | QFont, text: str) -> int:
    metrics = QFontMetrics(target if isinstance(target, QFont) else target.font())
    return metrics.horizontalAdvance(text)


def average_chars(target: QWidget | QFont, count: int) -> int:
    metrics = QFontMetrics(target if isinstance(target, QFont) else target.font())
    return metrics.averageCharWidth() * count


def control_height(target: QWidget | QFont, scale: float = 1.0) -> int:
    """Button height: one text line plus padding proportional to the font."""
    height = line_height(target)
    return int(round(height * (1.0 + 0.9 * scale)))


def set_role(widget: QWidget, role: str) -> QWidget:
    widget.setProperty(ROLE, role)
    repolish(widget)
    return widget


def set_kind(widget: QWidget, kind: str) -> QWidget:
    widget.setProperty(KIND, kind)
    repolish(widget)
    return widget


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# --- palette and stylesheet ----------------------------------------------------


def build_palette() -> QPalette:
    c = COLOR
    palette = QPalette()
    entries = {
        QPalette.ColorRole.Window: c.window_bg,
        QPalette.ColorRole.WindowText: c.text,
        QPalette.ColorRole.Base: c.card_bg,
        QPalette.ColorRole.AlternateBase: c.window_bg,
        QPalette.ColorRole.Text: c.text,
        QPalette.ColorRole.Button: c.secondary_bg,
        QPalette.ColorRole.ButtonText: c.secondary_text,
        QPalette.ColorRole.ToolTipBase: c.card_bg,
        QPalette.ColorRole.ToolTipText: c.text,
        QPalette.ColorRole.PlaceholderText: c.muted,
        QPalette.ColorRole.Highlight: c.selection,
        QPalette.ColorRole.HighlightedText: c.text,
        QPalette.ColorRole.Link: c.accent,
    }
    for role, value in entries.items():
        palette.setColor(role, QColor(value))
    return palette


def build_stylesheet() -> str:
    c, s, r = COLOR, SPACE, RADIUS
    return f"""
QMainWindow, QWidget#central {{
    background: {c.window_bg};
}}
QFrame[{KIND}="card"] {{
    background: {c.card_bg};
    border: 1px solid {c.border};
    border-radius: {r.card}px;
}}
QFrame[{KIND}="card"] QLabel, QFrame[{KIND}="card"] QScrollArea,
QFrame[{KIND}="card"] QWidget#scrollContent {{
    background: transparent;
    border: none;
}}
QLabel[{ROLE}="title"] {{
    color: {c.text};
}}
QLabel[{ROLE}="hint"] {{
    color: {c.muted};
}}
QLabel[{ROLE}="message"] {{ color: {c.text}; }}
QLabel[{ROLE}="error"] {{ color: {c.danger}; font-weight: bold; }}
QLabel[{ROLE}="warn"] {{ color: {c.warn}; font-weight: bold; }}
QLabel[{ROLE}="done"] {{ color: {c.done}; font-weight: bold; }}
QLabel[{ROLE}="empty"] {{ color: {c.muted}; }}
QFrame[{KIND}="notice"] {{
    background: {c.notice_bg};
    border: 1px solid {c.notice_border};
    border-radius: {r.control}px;
}}
QFrame[{KIND}="notice"] QLabel {{
    color: {c.notice_text};
    background: transparent;
    border: none;
}}
QPlainTextEdit, QListView {{
    background: {c.card_bg};
    border: 1px solid {c.border};
    border-radius: {r.control}px;
    color: {c.text};
}}
QPlainTextEdit {{
    padding: {s.xs}px;
    selection-background-color: {c.selection};
    selection-color: {c.text};
}}
QPlainTextEdit:focus {{
    border: 2px solid {c.accent_focus};
}}
QPushButton {{
    background: {c.secondary_bg};
    color: {c.secondary_text};
    border: 1px solid {c.secondary_bg};
    border-radius: {r.control}px;
    padding: 0 {s.lg}px;
    font-weight: bold;
}}
QPushButton:hover {{ background: {c.secondary_hover}; border-color: {c.secondary_hover}; }}
QPushButton:focus {{ border: 2px solid {c.accent_focus}; }}
QPushButton[{KIND}="primary"], QPushButton[{KIND}="convert"] {{
    background: {c.accent};
    color: #ffffff;
    border: 2px solid {c.accent};
}}
QPushButton[{KIND}="primary"]:hover, QPushButton[{KIND}="convert"]:hover {{
    background: {c.accent_dark};
    border-color: {c.accent_dark};
}}
QPushButton[{KIND}="primary"]:focus, QPushButton[{KIND}="convert"]:focus {{
    border: 2px solid {c.text};
}}
QPushButton[{KIND}="link"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {c.accent};
    padding: 0 {s.xs}px;
    font-weight: normal;
    text-decoration: underline;
}}
QPushButton[{KIND}="link"]:hover {{ color: {c.accent_dark}; background: transparent; border-color: transparent; }}
QPushButton[{KIND}="link"]:focus {{ border: 1px solid {c.accent_focus}; }}
QPushButton[{KIND}="link"]:disabled {{ background: transparent; border-color: transparent; color: {c.muted}; }}
QPushButton:disabled, QPushButton[{KIND}="convert"]:disabled {{
    background: {c.disabled_bg};
    border-color: {c.disabled_bg};
    color: {c.disabled_text};
}}
QToolTip {{
    background: {c.card_bg};
    color: {c.text};
    border: 1px solid {c.border};
    padding: {s.xs}px;
}}
"""


def apply_theme(app: QApplication) -> None:
    """Same look on every PC: Fusion style, token palette, font, stylesheet."""
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    app.setPalette(build_palette())
    app.setFont(font(FONT.body))
    app.setStyleSheet(build_stylesheet())

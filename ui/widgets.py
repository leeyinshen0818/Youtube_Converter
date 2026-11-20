"""Small reusable widgets (patterns adapted from usb-track-order / photo-layout)."""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLayout,
    QLayoutItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from ui import theme
from ui.theme import FONT, SPACE


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps to the next line when the
    width runs out (Qt's flow-layout example). Supports height-for-width."""

    def __init__(self, parent: QWidget | None = None, spacing: int = SPACE.sm) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        width = height = 0
        visible = [item for item in self._items if not item.isEmpty()]
        for item in visible:
            hint = item.sizeHint()
            width += hint.width()
            height = max(height, hint.height())
        width += self._spacing * max(0, len(visible) - 1)
        margins = self.contentsMargins()
        return QSize(width, height) + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins: QMargins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _do_layout(self, rect: QRect, apply: bool) -> int:
        """Place items line by line, each line's items centered vertically."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        lines: list[list[tuple[QLayoutItem, QSize]]] = [[]]
        x = area.x()
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint().expandedTo(item.minimumSize())
            if lines[-1] and x + hint.width() > area.right() + 1:
                lines.append([])
                x = area.x()
            lines[-1].append((item, hint))
            x += hint.width() + self._spacing
        y = area.y()
        for line in lines:
            if not line:
                continue
            line_height = max(hint.height() for _item, hint in line)
            x = area.x()
            for item, hint in line:
                if apply:
                    item.setGeometry(QRect(QPoint(x, y + (line_height - hint.height()) // 2), hint))
                x += hint.width() + self._spacing
            y += line_height + self._spacing
        used = y - self._spacing if any(lines) else y
        return used - rect.y() + margins.bottom()


def fitted_size_hint(widget: QWidget, base: QSize) -> QSize:
    """Size hint for a top-level window whose layout wraps text: at least the
    minimum width, and exactly as tall as the layout needs at that width
    (Qt's default measures the height at the narrower hint width)."""
    layout = widget.layout()
    width = max(base.width(), widget.minimumWidth())
    if layout is None or not layout.hasHeightForWidth():
        return QSize(width, base.height())
    return QSize(width, layout.totalHeightForWidth(width))


def fit_to_content(window: QWidget) -> None:
    """Make a window with wrapping text exactly as tall as its content.

    The minimum height is the height needed at the minimum width (the
    tallest case), set explicitly because Qt's own minimum measures wrapped
    text at an even narrower width and leaves an empty gap.
    """
    layout = window.layout()
    if layout is None:
        return
    layout.activate()
    width = max(window.minimumWidth(), layout.minimumSize().width())
    height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
    window.setMinimumHeight(height)
    # An unshown widget reports a default width, so start at the minimum.
    current = max(window.width(), width) if window.isVisible() else width
    needed = layout.totalHeightForWidth(current) if layout.hasHeightForWidth() else height
    window.resize(current, max(height, needed))  # never below the minimum


class Card(QFrame):
    """White rounded panel with a vertical layout."""

    def __init__(self, parent: QWidget | None = None, margin: int = SPACE.lg) -> None:
        super().__init__(parent)
        theme.set_kind(self, "card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(margin, margin, margin, margin)
        self.layout_.setSpacing(SPACE.md)


class ScrollColumn(QScrollArea):
    """Vertical-only scroll area used as a safety net around column content.

    The content fills the height when it fits and scrolls when it does not.
    """

    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content.setObjectName("scrollContent")
        self.setWidget(content)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # Width follows the content; height may shrink (the area scrolls).
        content = self.widget()
        width = content.minimumSizeHint().width() if content else 0
        width += self.verticalScrollBar().sizeHint().width()
        return QSize(width, theme.line_height(self, 3))


class WrapLabel(QLabel):
    """Word-wrapped label that never forces its parent wider than ~12 chars."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # Layouts use heightForWidth() for the real height; one line here.
        width = theme.average_chars(self, 12)
        return QSize(width, theme.line_height(self) if self.text() else 0)


class ElidedLabel(QLabel):
    """Single-line label that elides with "…"; the full text is the tooltip."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setToolTip(text)
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self.font())
        margins = self.contentsMargins()
        return QSize(
            metrics.horizontalAdvance("Wwww…") + margins.left() + margins.right(),
            metrics.height() + margins.top() + margins.bottom(),
        )

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self.font())
        margins = self.contentsMargins()
        return QSize(
            metrics.horizontalAdvance(self.text()) + margins.left() + margins.right(),
            metrics.height() + margins.top() + margins.bottom(),
        )

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = QFontMetrics(self.font()).elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)


class NoticeBox(QFrame):
    """Calm yellow note. Wraps normally; ``set_compact(True)`` shows one
    elided line (full text on hover) for short windows."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme.set_kind(self, "notice")
        small = theme.font(FONT.small)
        self.full = WrapLabel(text)
        self.full.setFont(small)
        self.short = ElidedLabel(text)
        self.short.setFont(small)
        # Hidden widgets take no space, unlike pages of a QStackedLayout.
        self.short.hide()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE.md, SPACE.sm, SPACE.md, SPACE.sm)
        outer.addWidget(self.full)
        outer.addWidget(self.short)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    @property
    def compact(self) -> bool:
        return self.short.isVisibleTo(self)

    def set_compact(self, compact: bool) -> None:
        if compact != self.compact:
            self.full.setVisible(not compact)
            self.short.setVisible(compact)
            self.updateGeometry()

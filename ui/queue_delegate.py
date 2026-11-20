"""Paints one queue row, including its action button.

    | Title (elided)                       [Badge] [ Button ] |
    | progress bar / note / red message    ...                |
    | hint (only rows with a problem)                         |

``row_geometry`` is the single source of positions: painting and mouse
hit-testing both use it, so a click lands exactly where the button is drawn.
All sizes derive from font metrics; nothing is a fixed pixel position.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from core.jobqueue import ItemSnapshot, ItemStatus
from ui.queue_model import ITEM_ROLE, SAVING_ROLE
from ui.theme import BADGES, COLOR, FONT, SPACE, BadgeStyle

STRIPE_WIDTH = 3  # logical px, like the old UI's left border
BUSY_SEGMENT = 0.3  # share of the bar covered by the moving busy segment
NARROW_TITLE_CHARS = 14  # below this title width the badge moves to line 2


class RowAction(str, Enum):
    STOP = "stop"
    DOWNLOAD = "download"
    SHOW = "show"
    RETRY = "retry"


@dataclass(frozen=True)
class ActionSpec:
    action: RowAction
    label: str
    primary: bool = False
    enabled: bool = True


BUTTON_LABELS = ("Stop", "Download", "Saving…", "Show in folder", "Retry")


def action_for(item: ItemSnapshot, saving: bool = False) -> ActionSpec:
    status = item.status
    if status.is_active:
        return ActionSpec(RowAction.STOP, "Stop")
    if status is ItemStatus.READY:
        if item.staged_missing:
            return ActionSpec(RowAction.RETRY, "Retry")
        if saving:
            return ActionSpec(RowAction.DOWNLOAD, "Saving…", primary=True, enabled=False)
        return ActionSpec(RowAction.DOWNLOAD, "Download", primary=True)
    if status is ItemStatus.SAVED:
        return ActionSpec(RowAction.SHOW, "Show in folder")
    return ActionSpec(RowAction.RETRY, "Retry")


def problem_for(item: ItemSnapshot):
    """The FriendlyError to show on the row, if any."""
    if item.status is ItemStatus.FAILED:
        return item.error
    if item.status is ItemStatus.READY:
        return item.save_error
    return None


def second_line_text(item: ItemSnapshot, saving: bool = False, location: str = "Downloads") -> str:
    status = item.status
    problem = problem_for(item)
    if problem is not None:
        return problem.message
    if status is ItemStatus.WAITING:
        return "Waiting to start"
    if status is ItemStatus.DOWNLOADING:
        return "Starting" if item.percent is None else f"{item.percent:.0f}%"
    if status is ItemStatus.CONVERTING:
        return "Making the MP3"
    if status is ItemStatus.READY:
        return "Saving…" if saving else "Ready to download"
    if status is ItemStatus.SAVED:
        return f"Saved to {location}"
    if status is ItemStatus.STOPPED:
        return "Stopped"
    return "Something went wrong"


@dataclass(frozen=True)
class RowGeometry:
    title: QRect
    line2: QRect
    line3: QRect | None
    badge: QRect
    button: QRect


@dataclass(frozen=True)
class RowFonts:
    body: QFont
    small: QFont
    bold: QFont
    badge: QFont

    @classmethod
    def from_font(cls, font: QFont) -> "RowFonts":
        # Pin family and size explicitly: a copied font whose size is only
        # inherited gets re-resolved by QPainter.setFont against the paint
        # device's font, so the text would be drawn at another size than it
        # was measured (and elided) with.
        body = QFont(font)
        body.setFamilies(font.families() or [font.family()])
        body.setPointSizeF(font.pointSizeF())
        small = QFont(body)
        small.setPointSizeF(FONT.small)
        bold = QFont(body)
        bold.setWeight(QFont.Weight.Bold)
        badge = QFont(small)
        badge.setWeight(QFont.Weight.Bold)
        return cls(body, small, bold, badge)


class QueueDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.busy_position = 0.0  # 0..1, advanced by the panel's timer
        self.hover_row: int | None = None
        self.hover_button = False
        self.pressed_row: int | None = None
        self.save_location = "Downloads"

    # --- metrics -----------------------------------------------------------

    @staticmethod
    def badge_size(fonts: RowFonts) -> QSize:
        metrics = QFontMetrics(fonts.badge)
        width = max(metrics.horizontalAdvance(style.label) for style in BADGES.values())
        return QSize(width + 2 * SPACE.md, metrics.height() + SPACE.sm)

    @staticmethod
    def button_size(fonts: RowFonts) -> QSize:
        metrics = QFontMetrics(fonts.bold)
        width = max(metrics.horizontalAdvance(label) for label in BUTTON_LABELS)
        return QSize(width + 2 * SPACE.md, int(round(metrics.height() * 1.7)))

    @staticmethod
    def is_tall(item: ItemSnapshot) -> bool:
        return problem_for(item) is not None

    def row_height(self, item: ItemSnapshot | None, fonts: RowFonts) -> int:
        body = QFontMetrics(fonts.body).height()
        left = body + SPACE.xs + body
        if item is not None and self.is_tall(item):
            left += SPACE.xs + QFontMetrics(fonts.small).height()
        right = max(self.button_size(fonts).height(), self.badge_size(fonts).height())
        return SPACE.md + max(left, right) + SPACE.md

    def row_geometry(self, rect: QRect, item: ItemSnapshot, font: QFont) -> RowGeometry:
        fonts = RowFonts.from_font(font)
        body = QFontMetrics(fonts.body).height()
        small = QFontMetrics(fonts.small).height()
        button = self.button_size(fonts)
        badge = self.badge_size(fonts)

        left = rect.left() + STRIPE_WIDTH + SPACE.md
        right = rect.right() - SPACE.md
        top = rect.top() + SPACE.md
        middle = rect.top() + rect.height() // 2

        button_rect = QRect(right - button.width() + 1, middle - button.height() // 2,
                            button.width(), button.height())
        text_right = button_rect.left() - SPACE.md
        title_room = text_right - badge.width() - SPACE.md - left
        narrow = title_room < QFontMetrics(fonts.body).averageCharWidth() * NARROW_TITLE_CHARS

        line1 = max(body, badge.height()) if not narrow else body
        line2_top = top + line1 + SPACE.xs
        if narrow:
            # Badge at the start of line 2; the title gets the full width.
            badge_rect = QRect(left, line2_top + (body - badge.height()) // 2, badge.width(), badge.height())
            title = QRect(left, top, max(0, text_right - left), line1)
            line2 = QRect(badge_rect.right() + SPACE.md, line2_top,
                          max(0, text_right - badge_rect.right() - SPACE.md), body)
        else:
            badge_rect = QRect(text_right - badge.width() + 1, top + (line1 - badge.height()) // 2,
                               badge.width(), badge.height())
            title = QRect(left, top, max(0, badge_rect.left() - SPACE.md - left), line1)
            line2 = QRect(left, line2_top, max(0, text_right - left + 1), body)
        line3 = None
        if self.is_tall(item):
            line3 = QRect(left, line2.bottom() + 1 + SPACE.xs, max(0, text_right - left + 1), small)
        return RowGeometry(title, line2, line3, badge_rect, button_rect)

    def button_at(self, rect: QRect, item: ItemSnapshot, font: QFont, pos: QPoint) -> bool:
        return self.row_geometry(rect, item, font).button.contains(pos)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        fonts = RowFonts.from_font(option.font)
        item: ItemSnapshot | None = index.data(ITEM_ROLE)
        width = (
            self.badge_size(fonts).width()
            + self.button_size(fonts).width()
            + QFontMetrics(fonts.body).averageCharWidth() * NARROW_TITLE_CHARS
            + 4 * SPACE.md
        )
        return QSize(width, self.row_height(item, fonts))

    # --- painting ----------------------------------------------------------

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        item: ItemSnapshot | None = index.data(ITEM_ROLE)
        if item is None:
            return
        saving = bool(index.data(SAVING_ROLE))
        style = BADGES[item.status]
        fonts = RowFonts.from_font(option.font)
        rect: QRect = option.rect
        geometry = self.row_geometry(rect, item, option.font)
        row = index.row()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        background = QColor(COLOR.card_bg)
        if option.state & QStyle.StateFlag.State_MouseOver or self.hover_row == row:
            background = QColor(COLOR.window_bg)
        painter.fillRect(rect, background)
        painter.setPen(QPen(QColor(COLOR.row_divider), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.fillRect(QRect(rect.left(), rect.top(), STRIPE_WIDTH, rect.height()), QColor(style.stripe))

        # Line 1: title.
        painter.setFont(fonts.body)
        painter.setPen(QColor(COLOR.text))
        title = QFontMetrics(fonts.body).elidedText(item.display_title, Qt.TextElideMode.ElideRight, geometry.title.width())
        painter.drawText(geometry.title, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), title)

        self._paint_badge(painter, geometry.badge, style, fonts.badge)

        # Line 2 (and 3).
        problem = problem_for(item)
        if item.status in (ItemStatus.DOWNLOADING, ItemStatus.CONVERTING):
            self._paint_progress(painter, geometry.line2, item, fonts.small)
        elif problem is not None:
            self._draw_text(painter, geometry.line2, problem.message, fonts.bold, COLOR.danger)
            if geometry.line3 is not None:
                self._draw_text(painter, geometry.line3, problem.hint, fonts.small, COLOR.muted)
        else:
            text = second_line_text(item, saving, self.save_location)
            self._draw_text(painter, geometry.line2, text, fonts.small, COLOR.muted, Qt.TextElideMode.ElideMiddle)

        # Action button.
        spec = action_for(item, saving)
        hovered = self.hover_row == row and self.hover_button
        pressed = self.pressed_row == row and hovered
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        self._paint_button(painter, geometry.button, spec, fonts.bold, hovered, pressed, focused)
        painter.restore()

    @staticmethod
    def _draw_text(painter: QPainter, rect: QRect, text: str, font: QFont, color: str,
                   elide: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        painter.setFont(font)
        painter.setPen(QColor(color))
        text = QFontMetrics(font).elidedText(text, elide, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

    @staticmethod
    def _paint_badge(painter: QPainter, rect: QRect, style: BadgeStyle, badge_font: QFont) -> None:
        radius = rect.height() / 2
        painter.setPen(QPen(QColor(COLOR.border), 1) if style.outlined else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(style.background))
        painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setFont(badge_font)
        painter.setPen(QColor(style.text))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), style.label)

    @staticmethod
    def _paint_button(painter: QPainter, rect: QRect, spec: ActionSpec, font: QFont,
                      hovered: bool, pressed: bool, focused: bool) -> None:
        if not spec.enabled:
            background, text, border = COLOR.disabled_bg, COLOR.disabled_text, COLOR.disabled_bg
        elif spec.primary:
            background = COLOR.accent_dark if hovered else COLOR.accent
            if pressed:
                background = QColor(COLOR.accent_dark).darker(115).name()
            text, border = "#ffffff", background
        else:
            background = COLOR.secondary_hover if hovered else COLOR.secondary_bg
            if pressed:
                background = QColor(COLOR.secondary_hover).darker(108).name()
            text, border = COLOR.secondary_text, background
        if focused:
            border = COLOR.text if spec.primary else COLOR.accent_focus
        box = QRectF(rect).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(border), 2 if focused else 1))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(box, 6, 6)
        painter.setFont(font)
        painter.setPen(QColor(text))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), spec.label)

    def _paint_progress(self, painter: QPainter, rect: QRect, item: ItemSnapshot, small: QFont) -> None:
        metrics = QFontMetrics(small)
        label = second_line_text(item)
        label_width = max(metrics.horizontalAdvance("100%"), metrics.horizontalAdvance(label))
        bar_height = max(4, metrics.height() // 3)
        bar = QRectF(rect.left(), rect.center().y() - bar_height / 2,
                     max(0, rect.width() - label_width - SPACE.md), bar_height)
        radius = bar_height / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLOR.track))
        painter.drawRoundedRect(bar, radius, radius)
        painter.setBrush(QColor(COLOR.accent))
        if item.status is ItemStatus.DOWNLOADING and item.percent is not None:
            filled = QRectF(bar.left(), bar.top(), bar.width() * max(0.0, min(item.percent, 100.0)) / 100.0, bar.height())
            if filled.width() > 0:
                painter.drawRoundedRect(filled, radius, radius)
        else:
            segment = bar.width() * BUSY_SEGMENT
            start = bar.left() + (bar.width() + segment) * self.busy_position - segment
            busy = QRectF(max(bar.left(), start), bar.top(), 0, bar.height())
            busy.setRight(min(bar.right(), start + segment))
            if busy.width() > 0:
                painter.drawRoundedRect(busy, radius, radius)
        painter.setFont(small)
        painter.setPen(QColor(COLOR.muted))
        text_rect = QRect(int(bar.right()) + SPACE.md, rect.top(), label_width, rect.height())
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), label)

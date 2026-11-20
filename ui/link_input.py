"""Multi-line link input with problem highlighting.

``core.urls`` reports positions as Python string indexes (code points).
Qt text positions count UTF-16 code units, so a character outside the Basic
Multilingual Plane (e.g. an emoji) counts as 2 in Qt. ``Utf16Map`` converts.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QMimeData, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QToolTip, QWidget

from ui import theme
from ui.theme import COLOR, FONT, MONO_FAMILY

PLACEHOLDER = "Paste YouTube links here, one per line"


class Utf16Map:
    """Convert Python string indexes to UTF-16 offsets for one text."""

    def __init__(self, text: str) -> None:
        # Python indexes of characters that take two UTF-16 units.
        self._wide = [index for index, char in enumerate(text) if ord(char) > 0xFFFF]

    def to_utf16(self, index: int) -> int:
        return index + bisect_left(self._wide, index)


def is_single_link(text: str) -> bool:
    """One piece of text with no line break or inner space, not ending in a
    newline (pasted text that already ends in one is left alone)."""
    stripped = text.strip()
    return bool(stripped) and "\n" not in text and not any(char.isspace() for char in stripped)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class Problem:
    """A highlighted range in UTF-16 positions, with the reason to show."""

    start: int
    end: int
    message: str


class LinkInput(QPlainTextEdit):
    convertRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(PLACEHOLDER)
        self.setFont(theme.font(FONT.input, family=MONO_FAMILY))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabChangesFocus(True)
        self.setMouseTracking(True)
        self.setAccessibleName("YouTube links")
        # (cursor selecting the range, reason, the text it selected)
        self._problems: list[tuple[QTextCursor, str, str]] = []
        # textChanged fires after Qt has moved the highlight cursors
        # (contentsChange fires too early for that).
        self.textChanged.connect(self._drop_edited_problems)

    # --- keys and paste -------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.accept()
            self.convertRequested.emit()
            return
        super().keyPressEvent(event)  # plain Enter inserts a newline

    def insert_links(self, text: str) -> None:
        """Insert pasted text at the cursor, on its own line(s)."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            return
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            cursor.removeSelectedText()
        single = is_single_link(text)
        if cursor.positionInBlock() > 0:
            text = "\n" + text  # start on a new line
        rest_of_line = cursor.block().text()[cursor.positionInBlock():]
        if rest_of_line.strip() and not text.endswith("\n"):
            text += "\n"  # keep what was after the cursor on its own line
        elif single and not rest_of_line.strip():
            text += "\n"  # fresh empty line, ready for the next link
        cursor.insertText(text)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802
        """Ctrl+V / middle-click paste. A single link pasted at the end of a
        line (or of the box) is followed by a newline, so the cursor waits on
        a fresh empty line. Everything else pastes exactly as Qt does."""
        if source.hasText():
            text = source.text().replace("\r\n", "\n").replace("\r", "\n")
            cursor = self.textCursor()
            if is_single_link(text):
                cursor.beginEditBlock()
                if cursor.hasSelection():
                    cursor.removeSelectedText()
                at_line_end = not cursor.block().text()[cursor.positionInBlock():].strip()
                cursor.insertText(text + "\n" if at_line_end else text)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                self.ensureCursorVisible()
                return
        super().insertFromMimeData(source)

    # --- text replacement -----------------------------------------------------

    def replace_all_text(self, text: str) -> None:
        """Replace the whole text as one undoable edit; cursor goes to the end."""
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    # --- problem highlighting -------------------------------------------------

    def set_problems(self, problems: list[Problem]) -> None:
        self._problems = []
        length = self.document().characterCount() - 1
        for problem in problems:
            start = max(0, min(problem.start, length))
            end = max(start, min(problem.end, length))
            if end <= start:
                continue
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self._problems.append((cursor, problem.message, cursor.selectedText()))
        self._refresh_selections()

    def clear_problems(self) -> None:
        if self._problems:
            self._problems = []
            self._refresh_selections()

    def problems(self) -> list[Problem]:
        return [
            Problem(cursor.selectionStart(), cursor.selectionEnd(), message)
            for cursor, message, _text in self._problems
        ]

    def problem_at(self, position: int) -> str | None:
        for cursor, message, _text in self._problems:
            if cursor.selectionStart() <= position < cursor.selectionEnd():
                return message
        return None

    def _refresh_selections(self) -> None:
        char_format = QTextCharFormat()
        char_format.setBackground(QColor(COLOR.danger_bg))
        char_format.setUnderlineColor(QColor(COLOR.danger))
        char_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        selections = []
        for cursor, _message, _text in self._problems:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(cursor)
            selection.format = char_format
            selections.append(selection)
        self.setExtraSelections(selections)

    def _drop_edited_problems(self) -> None:
        """Editing a highlighted range removes its highlight (cursors follow
        edits elsewhere, so only a change of the selected text counts)."""
        if not self._problems:
            return
        kept = []
        changed = False
        for cursor, message, original in self._problems:
            current = cursor.selectedText()
            extra = current[len(original):] if current.startswith(original) else ""
            if extra and (extra[0].isspace() or extra[0] == " "):
                # A new line or space typed right after a highlight joins Qt's
                # selection; trim it back so only the original range stays
                # marked. (Typing letters there edits the word: dropped below.)
                start = cursor.selectionStart()
                cursor.setPosition(start)
                cursor.setPosition(start + utf16_length(original), QTextCursor.MoveMode.KeepAnchor)
                current = cursor.selectedText()
                changed = True
            if current == original:
                kept.append((cursor, message, original))
            else:
                changed = True
        if changed:
            self._problems = kept
            self._refresh_selections()

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ToolTip:
            position = self.cursorForPosition(event.pos()).position()
            message = self.problem_at(position)
            if message is None and position > 0:
                message = self.problem_at(position - 1)  # hovering the last char
            if message:
                QToolTip.showText(event.globalPos(), message, self.viewport())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().viewportEvent(event)


__all__ = ["LinkInput", "Problem", "Utf16Map", "is_single_link", "utf16_length", "PLACEHOLDER"]

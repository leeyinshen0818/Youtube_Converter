"""Left column: "1. Paste links" and the "2. Convert" button."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.urls import MAX_LINKS, ParseResult, SkipReason, parse_links
from ui import theme
from ui.link_input import LinkInput, Problem, Utf16Map
from ui.theme import FONT, SPACE
from ui.widgets import Card, NoticeBox, ScrollColumn, WrapLabel

PERMISSION_NOTICE = (
    "Use this only for videos you own, public-domain content, Creative Commons "
    "content, or material you have permission to download."
)
COUNTER_DELAY_MS = 150

_REASON_WORDS: dict[SkipReason, tuple[str, str]] = {
    SkipReason.INVALID: ("not a link", "not links"),
    SkipReason.NOT_YOUTUBE: ("not a YouTube link", "not YouTube links"),
    SkipReason.PLAYLIST: ("playlist", "playlists"),
    SkipReason.CHANNEL: ("channel page", "channel pages"),
    SkipReason.ALBUM: ("album page", "album pages"),
    SkipReason.DUPLICATE: ("already in the list", "already in the list"),
    SkipReason.OVER_LIMIT: (
        f"over the {MAX_LINKS}-link limit (press Convert again for it)",
        f"over the {MAX_LINKS}-link limit (press Convert again for them)",
    ),
}


def plural(count: int, singular: str, plural_form: str) -> str:
    return f"{count} {singular if count == 1 else plural_form}"


def counter_text(result: ParseResult) -> tuple[str, str]:
    """Live counter text and its label role ("hint" or "warn")."""
    counts = result.skipped_counts()
    ready = len(result.valid) + counts.get(SkipReason.OVER_LIMIT, 0)
    parts = [plural(ready, "link ready", "links ready")]
    not_usable = len(result.input_errors)
    if not_usable:
        parts.append(f"{not_usable} not usable")
    if ready > MAX_LINKS:
        parts.append(f"only the first {MAX_LINKS} will be converted now")
        return " · ".join(parts), "warn"
    return " · ".join(parts), "hint"


def result_message(queued: int, result: ParseResult) -> tuple[str, str]:
    """Message after Convert, e.g. "2 queued, 3 skipped: 1 not a link, ..."."""
    counts = result.skipped_counts()
    skipped = sum(counts.values())
    if not queued and not skipped:
        return "", "message"
    head = f"{queued} queued" if queued else "Nothing queued"
    if not skipped:
        return head, "message"
    details = ", ".join(
        f"{counts[reason]} {_REASON_WORDS[reason][0 if counts[reason] == 1 else 1]}"
        for reason in SkipReason
        if counts.get(reason)
    )
    role = "error" if result.input_errors else "message"
    return f"{head}, {skipped} skipped: {details}", role


def text_after_queueing(text: str, result: ParseResult) -> str:
    """Remove queued links and duplicates; drop lines left empty by that.

    Bad links and links over the limit stay where they were.
    """
    spans = [link.span for link in result.valid]
    spans += [item.span for item in result.skipped if item.reason is SkipReason.DUPLICATE]
    if not spans:
        return text
    touched_lines = {span.line for span in spans}
    pieces: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda span: span.start):
        pieces.append(text[cursor:span.start])
        cursor = span.end
    pieces.append(text[cursor:])
    remaining = "".join(pieces)

    lines = remaining.split("\n")
    kept = [
        line.rstrip() if index in touched_lines else line
        for index, line in enumerate(lines)
        if not (index in touched_lines and not line.strip())
    ]
    return "\n".join(kept).strip("\n")


def problems_for(text: str) -> list[Problem]:
    """Highlight ranges (UTF-16) for every line the user has to fix."""
    result = parse_links(text)
    mapping = Utf16Map(text)
    return [
        Problem(mapping.to_utf16(item.span.start), mapping.to_utf16(item.span.end), item.message)
        for item in result.input_errors
    ]


class InputPanel(Card):
    # Emitted with list[ValidLink] when Convert is pressed and links are valid.
    # A signal (not a stored callback) so the panel holds no reference to the
    # window: Python reference cycles through Qt objects can crash when the
    # garbage collector frees them.
    linksSubmitted = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inputPanel")
        small = theme.font(FONT.small)

        # Scrollable part (safety net): title and input.
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE.sm)

        self.title = QLabel("1. Paste links")
        self.title.setFont(theme.font(FONT.title, bold=True))
        theme.set_role(self.title, "title")
        column.addWidget(self.title)

        self.input = LinkInput()
        self.input.setMinimumHeight(theme.line_height(self.input, 4) + 2 * SPACE.sm)
        self.input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        column.addWidget(self.input, 1)
        self.scroll = ScrollColumn(content)
        self.layout_.addWidget(self.scroll, 1)

        # Pinned part, always visible: buttons, counter, message, Convert,
        # hint and notice.
        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE.sm)
        self.paste_button = QPushButton("Paste")
        self.paste_button.setToolTip("Add the copied links at the cursor")
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Empty the box")
        for button in (self.paste_button, self.clear_button):
            button.setMinimumHeight(theme.control_height(button))
            button.setMinimumWidth(theme.text_width(button, button.text()) + 2 * SPACE.lg + SPACE.sm)
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.layout_.addLayout(buttons)

        self.counter = WrapLabel()
        theme.set_role(self.counter, "hint")
        self.layout_.addWidget(self.counter)

        self.message = WrapLabel()
        theme.set_role(self.message, "message")
        self.message.hide()
        self.layout_.addWidget(self.message)

        self.convert_button = QPushButton("2. Convert")
        self.convert_button.setFont(theme.font(FONT.convert, bold=True))
        theme.set_kind(self.convert_button, "convert")
        self.convert_button.setMinimumHeight(theme.control_height(self.convert_button, 1.3))
        self.convert_button.setMinimumWidth(
            theme.text_width(self.convert_button, self.convert_button.text()) + 2 * SPACE.lg
        )
        self.convert_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.convert_button.setDefault(False)
        self.convert_button.setAutoDefault(False)
        self.convert_button.setToolTip("Convert the links (Ctrl+Enter)")
        self.layout_.addWidget(self.convert_button)

        self.hint = QLabel("Ctrl+Enter also converts")
        self.hint.setFont(small)
        theme.set_role(self.hint, "hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.layout_.addWidget(self.hint)

        self.notice = NoticeBox(PERMISSION_NOTICE)
        self.layout_.addWidget(self.notice)

        self._counter_timer = QTimer(self)
        self._counter_timer.setSingleShot(True)
        self._counter_timer.setInterval(COUNTER_DELAY_MS)
        self._counter_timer.timeout.connect(self.update_counter)

        self.input.textChanged.connect(self._on_text_changed)
        self.input.convertRequested.connect(self.convert)
        self.convert_button.clicked.connect(self.convert)
        self.paste_button.clicked.connect(self.paste)
        self.clear_button.clicked.connect(self.clear)

        self.setTabOrder(self.input, self.paste_button)
        self.setTabOrder(self.paste_button, self.clear_button)
        self.setTabOrder(self.clear_button, self.convert_button)
        self.update_counter()

    # --- actions -----------------------------------------------------------

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        self.input.setFocus()
        self.input.insert_links(text)

    def clear(self) -> None:
        self.input.clear_problems()
        self.input.replace_all_text("")
        self._set_message("", "message")
        self.input.setFocus()

    def convert(self) -> None:
        text = self.input.toPlainText()
        result = parse_links(text)
        queued = 0
        if result.valid:
            self.linksSubmitted.emit(list(result.valid))
            queued = len(result.valid)
            remaining = text_after_queueing(text, result)
            if remaining != text:
                self.input.replace_all_text(remaining)
                text = remaining
        self.input.set_problems(problems_for(text))
        self._set_message(*result_message(queued, result))
        self.update_counter()
        self.input.setFocus()

    # --- state -------------------------------------------------------------

    def update_counter(self) -> None:
        text = self.input.toPlainText()
        result = parse_links(text)
        message, role = counter_text(result)
        self.counter.setText(message)
        theme.set_role(self.counter, role)
        self.convert_button.setEnabled(bool(text.strip()))

    def set_compact(self, compact: bool) -> None:
        """Short windows: the notice becomes one line (full text on hover)
        and the Ctrl+Enter hint moves into the Convert button's tooltip."""
        self.notice.set_compact(compact)
        self.hint.setVisible(not compact)

    def _on_text_changed(self) -> None:
        self.convert_button.setEnabled(bool(self.input.toPlainText().strip()))
        self._counter_timer.start()

    def _set_message(self, text: str, role: str) -> None:
        self.message.setText(text)
        theme.set_role(self.message, role)
        self.message.setVisible(bool(text))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.update_counter()


__all__ = [
    "InputPanel",
    "counter_text",
    "result_message",
    "text_after_queueing",
    "problems_for",
    "PERMISSION_NOTICE",
]

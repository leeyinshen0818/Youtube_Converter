"""Long titles, URLs and failure messages: rows keep a predictable height and
the painted title / badge / button / message never overlap (offscreen)."""

import unittest
from dataclasses import replace

from tests.qt_support import qt_app

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402

from core.converter import Phase  # noqa: E402
from core.errors import FriendlyError, ErrorKind, classify_error  # noqa: E402
from core.jobqueue import ItemSnapshot  # noqa: E402
from ui import theme  # noqa: E402
from ui.queue_delegate import BADGES, QueueDelegate, RowFonts, action_for  # noqa: E402

LONG_TITLE = (
    "【官方MV】周杰倫 Jay Chou【告白氣球 Love Confession】超長標題測試 Official Music Video "
    "4K Remastered Extended Version with Lyrics 中文字幕 English Subtitles (2016)"
)
LONG_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL" + "x" * 120 + "&index=42&t=1234s"
LONG_ERROR = FriendlyError(
    kind=ErrorKind.UNKNOWN,
    message="Something went wrong with this video and the explanation is unusually long " * 3,
    hint="Press Retry. If it keeps failing, click \"Downloads not working? Update\". " * 3,
    details="x",
)


def snap(**changes) -> ItemSnapshot:
    base = ItemSnapshot(
        id="a", batch_id="b", batch_created=1.0, url=LONG_URL, video_id="dQw4w9WgXcQ",
        title="Song", phase=Phase.WAITING, percent=None, error=None,
    )
    return replace(base, **changes)


def variants() -> dict[str, ItemSnapshot]:
    return {
        "long title waiting": snap(title=LONG_TITLE),
        "long title downloading": snap(title=LONG_TITLE, phase=Phase.DOWNLOADING, percent=42.0),
        "long title converting": snap(title=LONG_TITLE, phase=Phase.CONVERTING),
        "long url (no title)": snap(title=""),
        "long title ready": snap(title=LONG_TITLE, phase=Phase.DONE, staged_path=None),
        "long error": snap(title=LONG_TITLE, phase=Phase.FAILED, error=LONG_ERROR),
        "real error": snap(title=LONG_TITLE, phase=Phase.FAILED,
                           error=classify_error("ERROR: Sign in to confirm you're not a bot")),
        "short title": snap(title="Hi"),
    }


class LongTextRowTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.delegate = QueueDelegate()
        self.font = theme.font(theme.FONT.body)
        self.fonts = RowFonts.from_font(self.font)
        metrics = QFontMetrics(self.fonts.body)
        self.min_width = (
            self.delegate.badge_size(self.fonts).width()
            + self.delegate.button_size(self.fonts).width()
            + metrics.averageCharWidth() * 14
            + 4 * theme.SPACE.md
        )

    def test_row_height_depends_only_on_problem_state(self):
        normal = self.delegate.row_height(snap(title="Hi"), self.fonts)
        tall = self.delegate.row_height(snap(phase=Phase.FAILED, error=LONG_ERROR), self.fonts)
        self.assertGreater(tall, normal)
        for name, item in variants().items():
            with self.subTest(name=name):
                expected = tall if item.phase is Phase.FAILED else normal
                self.assertEqual(self.delegate.row_height(item, self.fonts), expected)

    def test_parts_never_overlap_at_any_width(self):
        for name, item in variants().items():
            height = self.delegate.row_height(item, self.fonts)
            for width in range(self.min_width, 1400, 37):
                rect = QRect(0, 0, width, height)
                g = self.delegate.row_geometry(rect, item, self.font)
                parts = {"title": g.title, "line2": g.line2, "badge": g.badge, "button": g.button}
                if g.line3 is not None:
                    parts["line3"] = g.line3
                with self.subTest(name=name, width=width):
                    names = list(parts)
                    for i, first in enumerate(names):
                        for second in names[i + 1:]:
                            a, b = parts[first], parts[second]
                            if a.width() and b.width():
                                self.assertFalse(a.intersects(b), f"{first} overlaps {second}")
                    for part, box in parts.items():
                        if box.width():
                            self.assertTrue(rect.contains(box), f"{part} outside the row")

    def test_button_and_badge_labels_fit(self):
        bold = QFontMetrics(self.fonts.bold)
        badge = QFontMetrics(self.fonts.badge)
        button = self.delegate.button_size(self.fonts)
        badge_size = self.delegate.badge_size(self.fonts)
        for item in variants().values():
            self.assertLessEqual(bold.horizontalAdvance(action_for(item).label), button.width())
        for style in BADGES.values():
            self.assertLessEqual(badge.horizontalAdvance(style.label), badge_size.width())

    def test_long_text_is_elided_to_its_box(self):
        item = variants()["long error"]
        rect = QRect(0, 0, self.min_width + 100, self.delegate.row_height(item, self.fonts))
        g = self.delegate.row_geometry(rect, item, self.font)
        body = QFontMetrics(self.fonts.body)
        elided = body.elidedText(LONG_TITLE, Qt.TextElideMode.ElideRight, g.title.width())
        self.assertLessEqual(body.horizontalAdvance(elided), g.title.width())
        self.assertTrue(elided.endswith("…"))


if __name__ == "__main__":
    unittest.main()


class RowFontTests(unittest.TestCase):
    """Windows gives item views a 9 pt class font; rows were measured with it
    but painted at 11 pt, so long titles lost their "…" and descenders were
    cut (only on the real Windows platform; see scripts/screenshot_matrix.py)."""

    def test_queue_view_font_is_set_explicitly(self):
        """An inherited font would let Windows' 9 pt item-view font win; an
        explicitly set one survives resolving against any other font."""
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QWidget

        from ui.queue_panel import QueueView

        qt_app()
        parent = QWidget()
        view = QueueView(parent)
        self.assertEqual(view.font().pointSizeF(), theme.FONT.body)
        resolved = view.font().resolve(QFont("Courier New", 9))
        self.assertEqual(resolved.pointSizeF(), theme.FONT.body)
        self.assertEqual(resolved.family(), view.font().family())

    def test_row_fonts_pin_family_and_size(self):
        from PySide6.QtGui import QFont

        qt_app()
        inherited = QFont()  # nothing set explicitly, like a class default
        fonts = RowFonts.from_font(inherited)
        device_font = QFont("Courier New", 20)  # what QPainter.setFont resolves against
        for font in (fonts.body, fonts.bold, fonts.small, fonts.badge):
            drawn = font.resolve(device_font)
            self.assertEqual(drawn.pointSizeF(), font.pointSizeF())
            self.assertEqual(drawn.family(), font.family())
        self.assertEqual(fonts.body.pointSizeF(), inherited.pointSizeF())

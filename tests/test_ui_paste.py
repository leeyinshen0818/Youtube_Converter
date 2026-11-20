"""Pasting a single link leaves the cursor on a fresh empty line."""

import unittest

from tests.qt_support import process_events, qt_app

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from ui.input_panel import InputPanel, problems_for  # noqa: E402
from ui.link_input import LinkInput, is_single_link  # noqa: E402

A = "https://youtu.be/dQw4w9WgXcQ"
B = "https://www.youtube.com/watch?v=9bZkp7q19f0"


class SingleLinkTests(unittest.TestCase):
    def test_is_single_link(self):
        self.assertTrue(is_single_link(A))
        self.assertTrue(is_single_link(f"  {A} "))
        self.assertFalse(is_single_link(f"{A}\n"))
        self.assertFalse(is_single_link(f"{A}\n{B}"))
        self.assertFalse(is_single_link(f"{A} {B}"))
        self.assertFalse(is_single_link("   "))


class CtrlVPasteTests(unittest.TestCase):
    """Ctrl+V goes through insertFromMimeData (as middle-click paste does)."""

    def setUp(self) -> None:
        qt_app()
        self.input = LinkInput()
        self.input.show()
        self.input.setFocus()
        process_events()

    def tearDown(self) -> None:
        self.input.close()

    def paste(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        QTest.keyClick(self.input, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    def place_cursor(self, position: int) -> None:
        cursor = self.input.textCursor()
        cursor.setPosition(position)
        self.input.setTextCursor(cursor)

    def cursor_line(self) -> tuple[int, str]:
        cursor = self.input.textCursor()
        return cursor.blockNumber(), cursor.block().text()

    def test_end_of_document(self):
        self.paste(A)
        self.assertEqual(self.input.toPlainText(), f"{A}\n")
        self.assertEqual(self.cursor_line(), (1, ""))
        self.paste(B)
        self.assertEqual(self.input.toPlainText(), f"{A}\n{B}\n")
        self.assertEqual(self.cursor_line(), (2, ""))

    def test_end_of_a_middle_line(self):
        self.input.setPlainText(f"{A}\n\n{B}")
        self.place_cursor(len(A) + 1)  # the empty middle line
        self.paste("https://youtu.be/kJQP7kiw5Fk")
        self.assertEqual(self.input.toPlainText(), f"{A}\nhttps://youtu.be/kJQP7kiw5Fk\n\n{B}")
        self.assertEqual(self.cursor_line(), (2, ""))

    def test_mid_line_gets_no_extra_newline(self):
        self.input.setPlainText("https://youtu.be/dQw4w9WgXcQ")
        self.place_cursor(len("https://youtu.be/"))
        self.paste("XYZ")
        self.assertEqual(self.input.toPlainText(), "https://youtu.be/XYZdQw4w9WgXcQ")

    def test_text_already_ending_in_newline(self):
        self.paste(f"{A}\n")
        self.assertEqual(self.input.toPlainText(), f"{A}\n")
        self.paste(f"{B}\r\n")
        self.assertEqual(self.input.toPlainText(), f"{A}\n{B}\n")

    def test_multi_line_paste_unchanged(self):
        self.paste(f"{A}\n{B}")
        self.assertEqual(self.input.toPlainText(), f"{A}\n{B}")

    def test_enter_and_ctrl_enter_still_work(self):
        fired = []
        self.input.convertRequested.connect(lambda: fired.append(True))
        self.paste(A)
        QTest.keyClick(self.input, Qt.Key.Key_Return)
        self.assertEqual(self.input.toPlainText(), f"{A}\n\n")
        QTest.keyClick(self.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(fired, [True])

    def test_paste_is_one_undo_step(self):
        self.paste(A)
        self.input.undo()
        self.assertEqual(self.input.toPlainText(), "")


class PanelPasteTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.panel = InputPanel()
        self.submitted = []
        self.panel.linksSubmitted.connect(self.submitted.append)
        self.panel.resize(420, 600)
        self.panel.show()
        process_events()

    def tearDown(self) -> None:
        self.panel.close()

    def click_paste(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        QTest.mouseClick(self.panel.paste_button, Qt.MouseButton.LeftButton)

    def test_paste_button_twice_then_convert(self):
        self.click_paste(A)
        self.assertTrue(self.panel.input.hasFocus())
        self.assertEqual(self.panel.input.toPlainText(), f"{A}\n")
        self.click_paste(B)
        self.assertEqual(self.panel.input.toPlainText(), f"{A}\n{B}\n")
        self.assertTrue(self.panel.input.hasFocus())
        self.assertEqual(self.panel.input.textCursor().block().text(), "")

        self.panel.update_counter()
        self.assertEqual(self.panel.counter.text(), "2 links ready")  # empty line not counted

        self.panel.convert()
        self.assertEqual([len(batch) for batch in self.submitted], [2])
        self.assertEqual(self.panel.input.toPlainText(), "")

    def test_paste_button_mid_line_keeps_existing_behaviour(self):
        self.panel.input.setPlainText("first")
        cursor = self.panel.input.textCursor()
        cursor.setPosition(2)
        self.panel.input.setTextCursor(cursor)
        self.click_paste(A)
        self.assertEqual(self.panel.input.toPlainText(), f"fi\n{A}\nrst")

    def test_trailing_empty_line_does_not_shift_highlights(self):
        self.click_paste("🎵bad")
        self.click_paste(A)
        text = self.panel.input.toPlainText()
        self.assertEqual(text, f"🎵bad\n{A}\n")
        self.panel.convert()
        self.assertEqual(self.panel.input.toPlainText(), "🎵bad")
        self.assertEqual(
            [selection.cursor.selectedText() for selection in self.panel.input.extraSelections()],
            ["🎵bad"],
        )
        self.assertEqual(problems_for("🎵bad\n"), problems_for("🎵bad"))


if __name__ == "__main__":
    unittest.main()

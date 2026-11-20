import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.qt_support import process_events, qt_app

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from core.converter import ConversionResult, Phase  # noqa: E402
from core.jobqueue import JobQueue  # noqa: E402
from core.urls import MAX_LINKS, parse_links  # noqa: E402
from ui.input_panel import (  # noqa: E402
    InputPanel,
    counter_text,
    problems_for,
    result_message,
    text_after_queueing,
)
from ui.link_input import LinkInput, Utf16Map, utf16_length  # noqa: E402
from ui.main_window import WINDOW_TITLE, MainWindow  # noqa: E402

A = "https://youtu.be/dQw4w9WgXcQ"
B = "https://www.youtube.com/watch?v=9bZkp7q19f0"
C = "https://music.youtube.com/watch?v=kJQP7kiw5Fk&list=RDAMVM"


def many(count: int) -> str:
    return "\n".join(f"https://youtu.be/vid{index:08d}" for index in range(count))


class PureHelperTests(unittest.TestCase):
    def test_utf16_map(self):
        text = "a🎵b😀c"
        mapping = Utf16Map(text)
        for index in range(len(text) + 1):
            self.assertEqual(mapping.to_utf16(index), utf16_length(text[:index]))

    def test_counter(self):
        self.assertEqual(counter_text(parse_links("")), ("0 links ready", "hint"))
        self.assertEqual(counter_text(parse_links(A)), ("1 link ready", "hint"))
        self.assertEqual(
            counter_text(parse_links(f"{A}\n{B}\n{C}\nnot a link")),
            ("3 links ready · 1 not usable", "hint"),
        )

    def test_counter_over_limit(self):
        text, role = counter_text(parse_links(many(MAX_LINKS + 3)))
        self.assertEqual(role, "warn")
        self.assertIn("53 links ready", text)
        self.assertIn(f"only the first {MAX_LINKS}", text)

    def test_result_message(self):
        result = parse_links(f"{A}\n{A}\nbad\nhttps://www.youtube.com/playlist?list=PL1")
        text, role = result_message(1, result)
        self.assertEqual(text, "1 queued, 3 skipped: 1 not a link, 1 playlist, 1 already in the list")
        self.assertEqual(role, "error")
        self.assertEqual(result_message(2, parse_links(f"{A}\n{B}")), ("2 queued", "message"))
        self.assertEqual(result_message(0, parse_links("bad")), ("Nothing queued, 1 skipped: 1 not a link", "error"))

    def test_text_after_queueing(self):
        text = f"{A}\nbad line\n  {B}  \n\n{A}\nkeep {C} me"
        result = parse_links(text)
        self.assertEqual(text_after_queueing(text, result), "bad line\n\nkeep  me")

    def test_over_limit_links_stay(self):
        text = many(MAX_LINKS + 2)
        remaining = text_after_queueing(text, parse_links(text))
        self.assertEqual(remaining.splitlines(), text.splitlines()[MAX_LINKS:])
        self.assertEqual(problems_for(remaining), [])

    def test_problems_with_emoji_before_bad_link(self):
        text = "🎵🎵 song\nhttps://www.youtube.com/@artist"
        problems = problems_for(text)
        units = text.encode("utf-16-le")
        selected = [units[p.start * 2:p.end * 2].decode("utf-16-le") for p in problems]
        self.assertEqual(selected, ["🎵🎵 song", "https://www.youtube.com/@artist"])


class LinkInputTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.input = LinkInput()
        self.input.show()
        self.input.setFocus()
        process_events()

    def tearDown(self) -> None:
        self.input.close()

    def test_enter_inserts_newline(self):
        QTest.keyClicks(self.input, "abc")
        QTest.keyClick(self.input, Qt.Key.Key_Return)
        QTest.keyClicks(self.input, "def")
        self.assertEqual(self.input.toPlainText(), "abc\ndef")

    def test_ctrl_enter_requests_convert(self):
        fired = []
        self.input.convertRequested.connect(lambda: fired.append(True))
        QTest.keyClicks(self.input, "abc")
        QTest.keyClick(self.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(self.input, Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(fired, [True, True])
        self.assertEqual(self.input.toPlainText(), "abc")

    def test_insert_links_at_cursor_on_own_line(self):
        self.input.setPlainText("first\nlast")
        cursor = self.input.textCursor()
        cursor.setPosition(len("first"))
        self.input.setTextCursor(cursor)
        self.input.insert_links(f"{A}\r\n{B}")
        self.assertEqual(self.input.toPlainText(), f"first\n{A}\n{B}\nlast")

    def test_problem_highlight_and_hover_text(self):
        text = "😀 x\nhttps://www.youtube.com/playlist?list=PL1"
        self.input.setPlainText(text)
        self.input.set_problems(problems_for(text))
        selections = self.input.extraSelections()
        self.assertEqual([s.cursor.selectedText() for s in selections],
                         ["😀 x", "https://www.youtube.com/playlist?list=PL1"])
        playlist_start = utf16_length("😀 x\n")
        self.assertIn("playlist", self.input.problem_at(playlist_start + 3))
        self.assertIsNone(self.input.problem_at(utf16_length("😀 x")))  # the newline

    def test_editing_a_problem_clears_only_its_highlight(self):
        text = "bad\nworse"
        self.input.setPlainText(text)
        self.input.set_problems(problems_for(text))
        cursor = self.input.textCursor()
        cursor.setPosition(1)
        cursor.insertText("X")
        self.assertEqual([s.cursor.selectedText() for s in self.input.extraSelections()], ["worse"])
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\nmore")  # a new line after a highlight keeps it
        self.assertEqual([s.cursor.selectedText() for s in self.input.extraSelections()], ["worse"])

    def test_typing_onto_the_end_of_a_bad_word_clears_it(self):
        self.input.setPlainText("youtu.be/abc")
        self.input.set_problems(problems_for("youtu.be/abc"))
        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("defghijk")
        self.assertEqual(self.input.extraSelections(), [])


class InputPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.submitted = []
        self.panel = InputPanel()
        self.panel.linksSubmitted.connect(self.submitted.append)
        self.panel.resize(420, 600)
        self.panel.show()
        process_events()

    def tearDown(self) -> None:
        self.panel.close()

    def test_convert_queues_and_removes_queued_lines(self):
        playlist = "https://www.youtube.com/playlist?list=PL1"
        self.panel.input.setPlainText(f"{A}\nnot a link\n{B}\n{A}\n{playlist}")
        self.panel.convert()
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual([link.video_id for link in self.submitted[0]], ["dQw4w9WgXcQ", "9bZkp7q19f0"])
        self.assertEqual(self.panel.input.toPlainText(), f"not a link\n{playlist}")
        self.assertEqual(
            [s.cursor.selectedText() for s in self.panel.input.extraSelections()],
            ["not a link", playlist],
        )
        self.assertEqual(
            self.panel.message.text(),
            "2 queued, 3 skipped: 1 not a link, 1 playlist, 1 already in the list",
        )
        self.assertFalse(self.panel.message.isHidden())

    def test_highlight_only_after_convert(self):
        QTest.keyClicks(self.panel.input, "not a link")
        process_events()
        self.assertEqual(self.panel.input.extraSelections(), [])
        self.panel.convert()
        self.assertEqual(len(self.panel.input.extraSelections()), 1)
        self.assertEqual(self.submitted, [])

    def test_over_limit_links_stay_unmarked(self):
        self.panel.input.setPlainText(many(MAX_LINKS + 5))
        self.panel.convert()
        self.assertEqual(len(self.submitted[0]), MAX_LINKS)
        self.assertEqual(len(self.panel.input.toPlainText().splitlines()), 5)
        self.assertEqual(self.panel.input.extraSelections(), [])

    def test_live_counter(self):
        self.panel.input.setPlainText(f"{A}\n{B}\nnope")
        QTest.qWait(250)
        self.assertEqual(self.panel.counter.text(), "2 links ready · 1 not usable")
        self.panel.input.setPlainText(many(60))
        QTest.qWait(250)
        self.assertIn("only the first 50", self.panel.counter.text())

    def test_paste_button_inserts_clipboard_at_cursor(self):
        QGuiApplication.clipboard().setText(f"{A}\n{B}")
        self.panel.input.setPlainText("keep me")
        cursor = self.panel.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.panel.input.setTextCursor(cursor)
        QTest.mouseClick(self.panel.paste_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.input.toPlainText(), f"keep me\n{A}\n{B}")
        self.assertTrue(self.panel.input.hasFocus())

    def test_clear(self):
        self.panel.input.setPlainText("bad")
        self.panel.convert()
        QTest.mouseClick(self.panel.clear_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.input.toPlainText(), "")
        self.assertEqual(self.panel.input.extraSelections(), [])
        self.assertTrue(self.panel.message.isHidden())

    def test_convert_button_disabled_when_empty(self):
        self.assertFalse(self.panel.convert_button.isEnabled())
        QTest.keyClicks(self.panel.input, "x")
        self.assertTrue(self.panel.convert_button.isEnabled())


class SlowFake:
    """Stays "downloading" until stopped."""

    def __init__(self, job_id, url, video_id, callbacks) -> None:
        self.callbacks = callbacks
        self.event = threading.Event()

    def run(self):
        self.callbacks.on_phase(Phase.DOWNLOADING)
        self.callbacks.on_progress(10.0)
        self.event.wait(30)
        return ConversionResult(Phase.STOPPED)

    def stop(self) -> None:
        time.sleep(0.5)  # a slow taskkill must not block the GUI thread
        self.event.set()


class MainWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = qt_app()
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp.name) / "tmp"
        (self.temp_root / "leftover-job").mkdir(parents=True)
        self.window = MainWindow(
            queue_factory=lambda bridge: JobQueue(SlowFake, **bridge.callbacks()),
            temp_root=self.temp_root,
        )
        # Answer "Quit" to the close dialog (a modal dialog would block tests).
        self.window.confirm = lambda *args: True

    def tearDown(self) -> None:
        self.window.queue.shutdown()
        self.window.queue.join(5)
        self.window.close()
        self.temp.cleanup()

    def test_title_and_no_menu_bar(self):
        self.assertEqual(self.window.windowTitle(), WINDOW_TITLE)
        self.assertTrue(WINDOW_TITLE.startswith("YouTube to MP3 Converter "))
        self.assertTrue(self.window.menuBar().actions() == [])

    def test_window_has_no_reference_cycle(self):
        # Python reference cycles through Qt objects crashed the garbage collector.
        import gc
        import weakref

        window = MainWindow(
            queue_factory=lambda bridge: JobQueue(SlowFake, **bridge.callbacks()),
            temp_root=self.temp_root,
        )
        ref = weakref.ref(window)
        gc.disable()
        try:
            del window
            self.assertIsNone(ref(), "MainWindow is kept alive by a reference cycle")
        finally:
            gc.enable()

    def test_stale_temp_folders_removed_on_start(self):
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_input_focused_when_shown_and_keys_work_without_click(self):
        self.window.resize(1000, 700)
        self.window.show()
        QTest.qWaitForWindowExposed(self.window)
        process_events(10)
        self.assertIs(self.app.focusWidget(), self.window.input_panel.input)
        focus = self.app.focusWidget()
        QTest.keyClicks(focus, A)
        QTest.keyClick(focus, Qt.Key.Key_Return)
        QTest.keyClicks(focus, B)
        QTest.keyClick(focus, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        process_events()
        self.assertEqual(self.window.model.rowCount(), 2)
        self.assertEqual(self.window.input_panel.input.toPlainText(), "")

    def test_close_stops_jobs_without_blocking_and_cleans_up(self):
        self.window.show()
        self.window.input_panel.input.setPlainText(f"{A}\n{B}\n{C}")
        self.window.input_panel.convert()
        deadline = time.monotonic() + 5
        while self.window.queue.counts()[Phase.DOWNLOADING] < 2 and time.monotonic() < deadline:
            process_events()
        (self.temp_root / "job-in-progress").mkdir(parents=True)

        started = time.monotonic()
        closed_now = self.window.close()
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertFalse(closed_now)  # waits for the jobs in the background
        self.assertEqual(self.window.queue.counts()[Phase.STOPPED], 3)

        deadline = time.monotonic() + 8
        while self.window.isVisible() and time.monotonic() < deadline:
            process_events()
            time.sleep(0.02)
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window.queue.is_idle())
        self.assertEqual(list(self.temp_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()


class CloseWithRealProcessesTests(unittest.TestCase):
    """Closing mid-download leaves no process and no temp work folder."""

    def test_close_kills_processes_and_removes_work_folders(self):
        import json
        import sys
        from unittest.mock import patch

        from core.converter import ConverterConfig
        from core.jobqueue import factory_for
        from tests.test_converter import process_alive

        qt_app()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            temp_root = root / "tmp"
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.write_bytes(b"")
            pid_file = root / "pids.json"
            fake = Path(__file__).with_name("fake_ytdlp.py")
            config = ConverterConfig(
                output_dir=root / "out",
                temp_root=temp_root,
                ytdlp=None,
                ffmpeg=ffmpeg,
                command_prefix=(sys.executable, str(fake), "sleep_download", "Song"),
            )
            window = MainWindow(
                queue_factory=lambda bridge: JobQueue(factory_for(lambda: config), **bridge.callbacks()),
                temp_root=temp_root,
            )
            window.confirm = lambda *args: True  # answer "Quit"
            window.show()
            with patch.dict(os.environ, {"FAKE_YTDLP_PIDS": str(pid_file)}):
                window.input_panel.input.setPlainText(A)
                window.input_panel.convert()
                deadline = time.monotonic() + 15
                while not pid_file.exists() and time.monotonic() < deadline:
                    process_events()
                    time.sleep(0.05)
            self.assertTrue(pid_file.exists())
            pids = json.loads(pid_file.read_text())
            self.assertTrue(any(temp_root.iterdir()))

            started = time.monotonic()
            window.close()
            self.assertLess(time.monotonic() - started, 0.3)
            deadline = time.monotonic() + 10
            while window.isVisible() and time.monotonic() < deadline:
                process_events()
                time.sleep(0.02)
            self.assertFalse(window.isVisible())
            for pid in pids:
                self.assertFalse(process_alive(pid), f"process {pid} still running")
            self.assertEqual(list(temp_root.iterdir()), [])
            window.queue.join(5)

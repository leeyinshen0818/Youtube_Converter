"""Stage 3c: ready/saved flow, row and header actions, dialogs (offscreen)."""

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from tests.qt_support import process_events, qt_app

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from core.converter import ConversionResult, Phase  # noqa: E402
from core.errors import ErrorKind, classify_error  # noqa: E402
from core.jobqueue import ItemSnapshot, ItemStatus, JobQueue  # noqa: E402
from core.saver import missing_file_error  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.messages import (  # noqa: E402
    RunTracker,
    all_done_text,
    clear_confirmation_text,
    close_warning_text,
    save_location_label,
    saved_text,
    summary_text,
)
from ui.queue_delegate import RowAction, action_for, second_line_text  # noqa: E402
from ui.queue_model import QueueModel  # noqa: E402
from ui.queue_panel import QueuePanel  # noqa: E402

LINKS = [f"https://youtu.be/vid{index:08d}" for index in range(10)]


def snapshot(item_id: str, **changes) -> ItemSnapshot:
    base = ItemSnapshot(
        id=item_id, batch_id="b", batch_created=1.0,
        url=f"https://www.youtube.com/watch?v={item_id:0>11}", video_id=f"{item_id:0>11}",
        title=f"Song {item_id}", phase=Phase.WAITING, percent=None, error=None,
    )
    return replace(base, **changes)


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process_events()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# --- pure helpers --------------------------------------------------------------


class MessageTests(unittest.TestCase):
    def test_summary_hides_zero_counts_and_uses_status_words(self):
        counts = {status: 0 for status in ItemStatus}
        counts.update({ItemStatus.DOWNLOADING: 2, ItemStatus.WAITING: 3, ItemStatus.READY: 5,
                       ItemStatus.SAVED: 1, ItemStatus.FAILED: 1})
        self.assertEqual(summary_text(counts), "2 downloading · 3 waiting · 5 ready · 1 saved · 1 failed")
        self.assertEqual(summary_text({status: 0 for status in ItemStatus}), "")

    def test_all_done(self):
        self.assertEqual(all_done_text(5), "All done: 5 ready to download")
        self.assertEqual(all_done_text(5, failed=1), "All done: 5 ready to download · 1 failed")
        self.assertEqual(all_done_text(0), "All done")

    def test_saved_text(self):
        self.assertEqual(saved_text(5, 0, "Downloads"), "5 MP3s saved to Downloads")
        self.assertEqual(saved_text(1, 0, "Downloads"), "1 MP3 saved to Downloads")
        self.assertEqual(saved_text(4, 1, "Music"), "4 MP3s saved to Music · 1 couldn't be saved")

    def test_save_location_label(self):
        from core.saver import default_save_dir

        self.assertEqual(save_location_label(default_save_dir()), "Downloads")
        self.assertEqual(save_location_label(Path("D:/Shop/Music")), "Music")

    def test_clear_confirmation(self):
        self.assertIsNone(clear_confirmation_text(0))
        self.assertEqual(clear_confirmation_text(1), "1 MP3 hasn't been downloaded yet. Clear anyway?")
        self.assertEqual(clear_confirmation_text(3), "3 MP3s haven't been downloaded yet. Clear anyway?")

    def test_close_warning(self):
        self.assertIsNone(close_warning_text(0, 0))
        self.assertEqual(
            close_warning_text(2, 3),
            "2 downloads are still running and 3 MP3s haven't been downloaded yet.",
        )
        self.assertEqual(close_warning_text(1, 0), "1 download is still running.")
        self.assertEqual(close_warning_text(0, 1), "1 MP3 hasn't been downloaded yet.")

    def test_run_tracker(self):
        run = RunTracker()
        self.assertIsNone(run.update([]))
        run.submitted(["a", "b", "c"])
        busy = [snapshot("a", phase=Phase.DONE, staged_path=Path("a.mp3")), snapshot("b", phase=Phase.DOWNLOADING),
                snapshot("c", phase=Phase.FAILED)]
        self.assertIsNone(run.update(busy))
        done = [busy[0], snapshot("b", phase=Phase.DONE, staged_path=Path("b.mp3")), busy[2],
                snapshot("old", phase=Phase.DONE, staged_path=Path("o.mp3"))]  # not in this run
        self.assertEqual(run.update(done), "All done: 2 ready to download · 1 failed")
        self.assertIsNone(run.update(done))  # only once per busy -> idle change
        run.retried("c")
        self.assertIsNone(run.update([snapshot("c", phase=Phase.WAITING)]))
        self.assertEqual(run.update([snapshot("c", phase=Phase.FAILED)]), "All done: 1 failed")
        run.submitted(["d"])  # new run starts fresh after idle
        self.assertEqual(run.ids, {"d"})


# --- delegate actions and hit-testing --------------------------------------------


class ActionTests(unittest.TestCase):
    def test_action_per_status(self):
        cases = [
            (snapshot("a"), RowAction.STOP, "Stop", True),
            (snapshot("a", phase=Phase.DOWNLOADING, percent=4.0), RowAction.STOP, "Stop", True),
            (snapshot("a", phase=Phase.CONVERTING), RowAction.STOP, "Stop", True),
            (snapshot("a", phase=Phase.DONE, staged_path=Path("a.mp3")), RowAction.DOWNLOAD, "Download", True),
            (snapshot("a", phase=Phase.DONE, staged_path=Path("a.mp3"), saved_path=Path("s.mp3")),
             RowAction.SHOW, "Show in folder", True),
            (snapshot("a", phase=Phase.FAILED, error=classify_error("x")), RowAction.RETRY, "Retry", True),
            (snapshot("a", phase=Phase.STOPPED), RowAction.RETRY, "Retry", True),
            (snapshot("a", phase=Phase.DONE, staged_path=Path("a.mp3"), staged_missing=True,
                      save_error=missing_file_error()), RowAction.RETRY, "Retry", True),
        ]
        for item, action, label, enabled in cases:
            with self.subTest(status=item.status, label=label):
                spec = action_for(item)
                self.assertEqual((spec.action, spec.label, spec.enabled), (action, label, enabled))
        ready = snapshot("a", phase=Phase.DONE, staged_path=Path("a.mp3"))
        self.assertTrue(action_for(ready).primary)
        saving = action_for(ready, saving=True)
        self.assertEqual((saving.label, saving.enabled), ("Saving…", False))

    def test_second_lines(self):
        ready = snapshot("a", phase=Phase.DONE, staged_path=Path("a.mp3"))
        self.assertEqual(second_line_text(ready), "Ready to download")
        saved = replace(ready, saved_path=Path("x.mp3"))
        self.assertEqual(second_line_text(saved, location="Downloads"), "Saved to Downloads")
        missing = replace(ready, staged_missing=True, save_error=missing_file_error())
        self.assertEqual(second_line_text(missing), "File is no longer available")


class HitTestTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.model = QueueModel()
        self.panel = QueuePanel(self.model)
        self.panel.resize(700, 1100)
        self.panel.show()
        self.view = self.panel.view
        self.actions: list[tuple[str, str]] = []
        self.panel.actionTriggered.connect(lambda item_id, action: self.actions.append((item_id, action)))

    def tearDown(self) -> None:
        self.panel.close()

    def button_center(self, row: int) -> QPoint:
        index = self.model.index(row)
        item = self.model.item(row)
        geometry = self.view.delegate.row_geometry(self.view.visualRect(index), item, self.view.font())
        return geometry.button.center()

    def test_click_on_button_triggers_its_action_per_status(self):
        items = [
            snapshot("w"),
            snapshot("d", phase=Phase.DOWNLOADING, percent=10.0),
            snapshot("c", phase=Phase.CONVERTING),
            snapshot("r", phase=Phase.DONE, staged_path=Path("r.mp3")),
            snapshot("s", phase=Phase.DONE, staged_path=Path("s.mp3"), saved_path=Path("S.mp3")),
            snapshot("f", phase=Phase.FAILED, error=classify_error("Video unavailable")),
            snapshot("x", phase=Phase.STOPPED),
        ]
        for item in items:
            self.model.apply(item)
        process_events()
        expected = ["stop", "stop", "stop", "download", "show", "retry", "retry"]
        for row, action in enumerate(expected):
            QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.button_center(row))
        self.assertEqual(self.actions, [(item.id, action) for item, action in zip(items, expected)])

    def test_click_outside_button_does_nothing(self):
        self.model.apply(snapshot("r", phase=Phase.DONE, staged_path=Path("r.mp3")))
        process_events()
        rect = self.view.visualRect(self.model.index(0))
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.topLeft() + QPoint(20, 10))
        self.assertEqual(self.actions, [])
        self.assertIsNone(self.view.button_hit(rect.topLeft() + QPoint(20, 10)))

    def test_disabled_button_while_saving(self):
        self.model.apply(snapshot("r", phase=Phase.DONE, staged_path=Path("r.mp3")))
        self.model.set_saving("r", True)
        process_events()
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.button_center(0))
        self.assertEqual(self.actions, [])

    def test_keyboard_arrows_and_space(self):
        for item_id in ("a", "b"):
            self.model.apply(snapshot(item_id, phase=Phase.DONE, staged_path=Path(f"{item_id}.mp3")))
        process_events()
        self.view.setFocus()
        process_events()
        self.assertEqual(self.view.currentIndex().row(), 0)
        QTest.keyClick(self.view, Qt.Key.Key_Down)
        QTest.keyClick(self.view, Qt.Key.Key_Space)
        QTest.keyClick(self.view, Qt.Key.Key_Up)
        QTest.keyClick(self.view, Qt.Key.Key_Return)
        self.assertEqual(self.actions, [("b", "download"), ("a", "download")])

    def test_header_download_all_visible_only_with_ready_items(self):
        self.model.apply(snapshot("a", phase=Phase.DOWNLOADING, percent=1.0))
        process_events()
        self.assertTrue(self.panel.download_all_button.isHidden())
        self.assertFalse(self.panel.clear_button.isEnabled())
        self.model.apply(snapshot("b", phase=Phase.DONE, staged_path=Path("b.mp3")))
        self.assertFalse(self.panel.download_all_button.isHidden())
        self.assertTrue(self.panel.clear_button.isEnabled())
        self.assertEqual(self.panel.summary.text(), "1 downloading · 1 ready")
        self.model.apply(snapshot("b", phase=Phase.DONE, staged_path=Path("b.mp3"), saved_path=Path("B.mp3")))
        self.assertTrue(self.panel.download_all_button.isHidden())


class ModelSavedTests(unittest.TestCase):
    def test_ready_to_saved_changes_only_that_row(self):
        qt_app()
        model = QueueModel()
        for item_id in ("a", "b", "c"):
            model.apply(snapshot(item_id, phase=Phase.DONE, staged_path=Path(f"{item_id}.mp3")))
        events = []
        model.dataChanged.connect(lambda tl, br, roles=None: events.append(("changed", tl.row(), br.row())))
        model.modelReset.connect(lambda: events.append(("reset",)))
        model.layoutChanged.connect(lambda *a: events.append(("layout",)))
        model.set_saving("b", True)
        model.apply(snapshot("b", phase=Phase.DONE, staged_path=Path("b.mp3"), saved_path=Path("B.mp3")))
        model.set_saving("b", False)
        self.assertEqual(events, [("changed", 1, 1)] * 3)
        self.assertEqual(model.item(1).status, ItemStatus.SAVED)


# --- window flows -----------------------------------------------------------------


class StagedFake:
    """Writes "<ready>/<job id>/<title>.mp3" and finishes, like a real conversion."""

    ready_root: Path = Path()
    outcome = "done"

    def __init__(self, job_id, url, video_id, callbacks) -> None:
        self.job_id, self.video_id, self.callbacks = job_id, video_id, callbacks
        self.stopped = threading.Event()

    def run(self):
        self.callbacks.on_phase(Phase.DOWNLOADING)
        if StagedFake.outcome == "hang":
            self.stopped.wait(30)
            return ConversionResult(Phase.STOPPED)
        title = "Same Title" if self.video_id.endswith(("1", "2")) else f"Song {self.video_id[-1]}"
        path = StagedFake.ready_root / self.job_id / f"{title}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.video_id.encode())
        return ConversionResult(Phase.DONE, title=title, output_path=path)

    def stop(self) -> None:
        self.stopped.set()


class FakeShell:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def open_folder(self, folder: Path) -> None:
        self.calls.append(("open", folder))

    def show_in_folder(self, path: Path) -> None:
        self.calls.append(("show", path))


class WindowFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ready = root / "ready"
        self.tmp = root / "tmp"
        self.save_dir = root / "Downloads" / "YouTube MP3 Converter"
        for leftover in (self.ready / "old-job" / "Old.mp3", self.ready / "stray.mp3", self.tmp / "old" / "x.part"):
            leftover.parent.mkdir(parents=True, exist_ok=True)
            leftover.write_bytes(b"old")
        StagedFake.ready_root = self.ready
        StagedFake.outcome = "done"
        self.shell = FakeShell()
        self.questions: list[str] = []
        self.answer = True
        self.window = self.make_window()

    def make_window(self) -> MainWindow:
        window = MainWindow(
            queue_factory=lambda bridge: JobQueue(StagedFake, staging_root=self.ready, **bridge.callbacks()),
            temp_root=self.tmp,
            ready_root=self.ready,
            save_dir=self.save_dir,
            shell=self.shell,
        )

        def confirm(text, title, accept, reject):
            self.questions.append(text)
            return self.answer

        window.confirm = confirm
        window.resize(1200, 800)
        window.show()
        return window

    def tearDown(self) -> None:
        self.answer = True
        self.window.close()
        wait_for(lambda: not self.window.isVisible(), 8)
        self.temp.cleanup()

    def convert(self, count: int) -> None:
        self.window.input_panel.input.setPlainText("\n".join(LINKS[:count]))
        self.window.input_panel.convert()
        self.assertTrue(wait_for(lambda: self.status_counts().get(ItemStatus.READY, 0) == count))

    def status_counts(self) -> dict:
        counts: dict = {}
        for item in self.window.model.items():
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def saved_files(self) -> list[str]:
        return sorted(path.name for path in self.save_dir.iterdir()) if self.save_dir.exists() else []

    def test_startup_cleans_ready_and_tmp(self):
        self.assertEqual(list(self.ready.iterdir()), [])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_converted_files_wait_until_download(self):
        self.convert(3)
        self.assertEqual(self.saved_files(), [])
        self.assertEqual(self.window.queue_panel.done_line.text(), "All done: 3 ready to download")
        first = self.window.model.item(0)
        self.window.handle_action(first.id, RowAction.DOWNLOAD.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).status is ItemStatus.SAVED))
        saved = self.window.model.item(0).saved_path
        self.assertEqual(saved.parent, self.save_dir)
        self.assertEqual(saved.read_bytes(), first.video_id.encode())
        self.assertFalse(first.staged_path.exists())
        self.assertFalse(first.staged_path.parent.exists())  # empty job folder removed

        self.window.handle_action(first.id, RowAction.SHOW.value)
        self.assertEqual(self.shell.calls, [("show", saved)])

    def test_download_all_and_name_clash(self):
        self.convert(3)  # videos 0, 1, 2: "Song 0", "Same Title", "Same Title"
        self.assertEqual(self.window.download_all(), 3)
        self.assertTrue(wait_for(lambda: self.status_counts().get(ItemStatus.SAVED) == 3))
        self.assertEqual(self.saved_files(), ["Same Title (2).mp3", "Same Title.mp3", "Song 0.mp3"])
        self.assertEqual(self.window.queue_panel.done_line.text(), "3 MP3s saved to Downloads")
        self.assertTrue(self.window.queue_panel.download_all_button.isHidden())
        self.assertEqual(self.window.download_all(), 0)

    def test_failed_save_keeps_staged_file(self):
        from core import saver

        self.convert(1)
        item = self.window.model.item(0)

        def full(staged, dest):
            raise OSError(28, "No space left on device")

        self.window.saver._save_item = full
        self.window.handle_action(item.id, RowAction.DOWNLOAD.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).save_error is not None))
        row = self.window.model.item(0)
        self.assertEqual(row.status, ItemStatus.READY)
        self.assertEqual(row.save_error.kind, ErrorKind.DISK_FULL)
        self.assertTrue(row.staged_path.exists())
        self.assertEqual(action_for(row).action, RowAction.DOWNLOAD)

        self.window.saver._save_item = saver.save_item  # space freed: try again
        self.window.handle_action(item.id, RowAction.DOWNLOAD.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).status is ItemStatus.SAVED))
        self.assertIsNone(self.window.model.item(0).save_error)

    def test_missing_staged_file_offers_retry(self):
        self.convert(1)
        item = self.window.model.item(0)
        item.staged_path.unlink()
        self.window.handle_action(item.id, RowAction.DOWNLOAD.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).staged_missing))
        row = self.window.model.item(0)
        self.assertEqual(second_line_text(row), "File is no longer available")
        self.assertEqual(action_for(row).action, RowAction.RETRY)
        self.window.handle_action(item.id, RowAction.RETRY.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).can_save))
        self.assertEqual(self.window.model.row_of(item.id), 0)  # row kept its place

    def test_clear_finished_asks_only_when_unsaved(self):
        self.convert(2)
        first = self.window.model.item(0)
        self.window.handle_action(first.id, RowAction.DOWNLOAD.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).status is ItemStatus.SAVED))
        staged = self.window.model.item(1).staged_path

        self.answer = False  # Cancel
        self.assertEqual(self.window.clear_finished(), [])
        self.assertEqual(self.questions, ["1 MP3 hasn't been downloaded yet. Clear anyway?"])
        self.assertEqual(self.window.model.rowCount(), 2)

        self.answer = True  # Clear anyway
        self.assertEqual(len(self.window.clear_finished()), 2)
        self.assertTrue(wait_for(lambda: self.window.model.rowCount() == 0))
        self.assertTrue(wait_for(lambda: not staged.exists()))
        self.assertEqual(len(self.saved_files()), 1)  # the saved MP3 stays

        self.questions.clear()
        self.convert(1)
        self.window.download_all()
        self.assertTrue(wait_for(lambda: self.status_counts().get(ItemStatus.SAVED) == 1))
        self.window.clear_finished()
        self.assertEqual(self.questions, [])  # nothing unsaved: no dialog

    def test_close_dialog_go_back_and_quit(self):
        StagedFake.outcome = "hang"
        self.window.input_panel.input.setPlainText("\n".join(LINKS[:2]))
        self.window.input_panel.convert()
        self.assertTrue(wait_for(lambda: self.status_counts().get(ItemStatus.DOWNLOADING) == 2))
        StagedFake.outcome = "done"

        self.answer = False  # Go back
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.questions, ["2 downloads are still running."])
        self.assertEqual(self.status_counts().get(ItemStatus.DOWNLOADING), 2)

        self.answer = True  # Quit
        self.window.close()
        self.assertTrue(wait_for(lambda: not self.window.isVisible(), 8))
        self.assertTrue(self.window.queue.is_idle())
        self.assertEqual(list(self.ready.iterdir()), [])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_close_with_unsaved_deletes_staged_files(self):
        self.convert(3)
        self.assertEqual(self.window.close_warning(), "3 MP3s haven't been downloaded yet.")
        self.window.close()
        self.assertTrue(wait_for(lambda: not self.window.isVisible(), 8))
        self.assertEqual(self.questions, ["3 MP3s haven't been downloaded yet."])
        self.assertEqual(list(self.ready.iterdir()), [])
        self.assertEqual(self.saved_files(), [])

    def test_no_close_dialog_when_nothing_at_stake(self):
        self.convert(1)
        self.window.download_all()
        self.assertTrue(wait_for(lambda: self.status_counts().get(ItemStatus.SAVED) == 1))
        self.assertIsNone(self.window.close_warning())
        self.window.close()
        self.assertTrue(wait_for(lambda: not self.window.isVisible(), 8))
        self.assertEqual(self.questions, [])

    def test_open_folder(self):
        QTest.mouseClick(self.window.queue_panel.open_folder_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.shell.calls, [("open", self.save_dir)])

    def test_tab_order(self):
        self.convert(1)
        panel, inputs = self.window.queue_panel, self.window.input_panel
        expected = [inputs.paste_button, inputs.clear_button, inputs.convert_button, panel.view,
                    panel.download_all_button, panel.open_folder_button, panel.clear_button]
        widget = inputs.input
        for target in expected:
            widget = widget.nextInFocusChain()
            while not widget.isVisible() or widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
                widget = widget.nextInFocusChain()
            self.assertIs(widget, target)


if __name__ == "__main__":
    unittest.main()

import unittest
from dataclasses import replace
from pathlib import Path

from tests.qt_support import process_events, qt_app

from PySide6.QtCore import Qt  # noqa: E402

from core.converter import Phase  # noqa: E402
from core.errors import classify_error  # noqa: E402
from core.jobqueue import ItemSnapshot, ItemStatus  # noqa: E402
from ui.queue_bridge import QueueBridge  # noqa: E402
from ui.queue_model import ITEM_ROLE, QueueModel, tooltip_for  # noqa: E402
from ui.queue_panel import EMPTY_TEXT, QueuePanel  # noqa: E402


def snapshot(item_id: str, **changes) -> ItemSnapshot:
    base = ItemSnapshot(
        id=item_id,
        batch_id="b1",
        batch_created=1.0,
        url=f"https://www.youtube.com/watch?v={item_id:0>11}",
        video_id=f"{item_id:0>11}",
        title="",
        phase=Phase.WAITING,
        percent=None,
        error=None,
        staged_path=None,
    )
    return replace(base, **changes)


class Spy:
    def __init__(self, model: QueueModel) -> None:
        self.events: list[tuple] = []
        model.dataChanged.connect(lambda tl, br, roles=None: self.events.append(("changed", tl.row(), br.row())))
        model.rowsInserted.connect(lambda parent, first, last: self.events.append(("inserted", first, last)))
        model.rowsRemoved.connect(lambda parent, first, last: self.events.append(("removed", first, last)))
        model.modelReset.connect(lambda: self.events.append(("reset",)))
        model.layoutChanged.connect(lambda *args: self.events.append(("layout",)))


class QueueModelTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.model = QueueModel()
        self.spy = Spy(self.model)

    def test_new_items_are_appended_in_order(self):
        for item_id in ("a", "b", "c"):
            self.model.apply(snapshot(item_id))
        self.assertEqual(self.spy.events, [("inserted", 0, 0), ("inserted", 1, 1), ("inserted", 2, 2)])
        self.assertEqual([self.model.item(row).id for row in range(3)], ["a", "b", "c"])

    def test_update_emits_data_changed_for_that_row_only(self):
        for item_id in ("a", "b", "c"):
            self.model.apply(snapshot(item_id))
        self.spy.events.clear()
        self.model.apply(snapshot("b", phase=Phase.DOWNLOADING, percent=40.0, title="Song B"))
        self.assertEqual(self.spy.events, [("changed", 1, 1)])
        index = self.model.index(1)
        self.assertEqual(index.data(ITEM_ROLE).percent, 40.0)
        self.assertEqual(index.data(Qt.ItemDataRole.DisplayRole), "Song B")

    def test_never_resets(self):
        for item_id in ("a", "b"):
            self.model.apply(snapshot(item_id))
        for percent in (10.0, 20.0, 30.0):
            self.model.apply(snapshot("a", phase=Phase.DOWNLOADING, percent=percent))
        self.model.apply(snapshot("a", phase=Phase.DONE))
        self.model.remove(["a"])
        kinds = {event[0] for event in self.spy.events}
        self.assertNotIn("reset", kinds)
        self.assertNotIn("layout", kinds)

    def test_remove_keeps_other_rows_addressable(self):
        for item_id in ("a", "b", "c", "d"):
            self.model.apply(snapshot(item_id))
        self.spy.events.clear()
        self.model.remove(["b", "missing", "d"])
        self.assertEqual(self.spy.events, [("removed", 1, 1), ("removed", 2, 2)])
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.row_of("c"), 1)
        self.assertIsNone(self.model.row_of("b"))
        self.spy.events.clear()
        self.model.apply(snapshot("c", phase=Phase.DONE))
        self.assertEqual(self.spy.events, [("changed", 1, 1)])

    def test_rows_in_status(self):
        self.model.apply(snapshot("a", phase=Phase.CONVERTING))
        self.model.apply(snapshot("b", phase=Phase.DOWNLOADING))
        self.model.apply(snapshot("c", phase=Phase.CONVERTING))
        self.assertEqual(self.model.rows_in_status(ItemStatus.CONVERTING), [0, 2])

    def test_failed_tooltip_has_details(self):
        error = classify_error("ERROR: [youtube] x: Video unavailable")
        item = snapshot("a", phase=Phase.FAILED, error=error, title="Song")
        tip = tooltip_for(item)
        self.assertIn("This video can't be opened", tip)
        self.assertIn("Details:", tip)
        self.assertIn("Video unavailable", tip)

    def test_done_tooltip_has_path(self):
        item = snapshot("a", phase=Phase.DONE, staged_path=Path("C:/Music/Song.mp3"))
        self.assertIn("Song.mp3", tooltip_for(item))

    def test_items_are_read_only(self):
        self.model.apply(snapshot("a"))
        flags = self.model.flags(self.model.index(0))
        self.assertFalse(flags & Qt.ItemFlag.ItemIsEditable)
        self.assertFalse(flags & Qt.ItemFlag.ItemIsSelectable)


class QueueBridgeTests(unittest.TestCase):
    def test_bridge_feeds_model(self):
        qt_app()
        bridge = QueueBridge()
        model = QueueModel()
        bridge.itemChanged.connect(model.apply)
        bridge.itemsRemoved.connect(model.remove)
        callbacks = bridge.callbacks()
        callbacks["on_item_changed"](snapshot("a"))
        callbacks["on_item_changed"](snapshot("a", phase=Phase.DONE))
        self.assertEqual(model.item(0).phase, Phase.DONE)
        callbacks["on_items_removed"](["a"])
        self.assertEqual(model.rowCount(), 0)

    def test_signals_from_worker_thread_arrive_on_gui_thread(self):
        import threading

        app = qt_app()
        bridge = QueueBridge()
        model = QueueModel()
        threads = []
        bridge.itemChanged.connect(lambda item: (threads.append(threading.current_thread()), model.apply(item)))
        worker = threading.Thread(target=lambda: bridge.on_item_changed(snapshot("a")))
        worker.start()
        worker.join()
        for _ in range(20):
            app.processEvents()
        self.assertEqual(threads, [threading.main_thread()])
        self.assertEqual(model.rowCount(), 1)


class QueuePanelTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.model = QueueModel()
        self.panel = QueuePanel(self.model)
        self.panel.resize(600, 400)
        self.panel.show()
        process_events()

    def tearDown(self) -> None:
        self.panel.close()

    def test_empty_state(self):
        self.assertIs(self.panel.stack.currentWidget(), self.panel.empty)
        self.assertEqual(self.panel.empty.text(), EMPTY_TEXT)
        self.model.apply(snapshot("a"))
        self.assertIs(self.panel.stack.currentWidget(), self.panel.view)
        self.model.remove(["a"])
        self.assertIs(self.panel.stack.currentWidget(), self.panel.empty)

    def test_busy_timer_runs_only_while_converting(self):
        self.model.apply(snapshot("a", phase=Phase.DOWNLOADING, percent=10.0))
        self.assertFalse(self.panel.busy_animation_running)
        self.model.apply(snapshot("a", phase=Phase.CONVERTING))
        self.assertTrue(self.panel.busy_animation_running)
        self.model.apply(snapshot("a", phase=Phase.DONE))
        self.assertFalse(self.panel.busy_animation_running)

    def test_failed_row_is_taller(self):
        self.model.apply(snapshot("a", phase=Phase.DOWNLOADING, percent=1.0))
        normal = self.panel.view.sizeHintForRow(0)
        self.model.apply(snapshot("a", phase=Phase.FAILED, error=classify_error("Video unavailable")))
        process_events()
        self.assertGreater(self.panel.view.sizeHintForRow(0), normal)
        self.assertGreater(self.panel.view.visualRect(self.model.index(0)).height(), normal)

    def test_reveal_scrolls_to_new_row(self):
        for index in range(40):
            self.model.apply(snapshot(f"i{index}"))
        process_events()
        self.panel.reveal("i30")
        process_events(10)
        rect = self.panel.view.visualRect(self.model.index(30))
        self.assertEqual(rect.top(), 0)


if __name__ == "__main__":
    unittest.main()

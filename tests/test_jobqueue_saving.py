"""JobQueue: ready vs saved, save marks, staging folders and orphan cleanup."""

import sys
import tempfile
import threading
import unittest
from pathlib import Path

from core.converter import ConversionResult, ConverterConfig, Phase
from core.errors import classify_error
from core.jobqueue import ItemStatus, JobQueue, factory_for
from core.saver import missing_file_error
from tests.test_jobqueue import Harness, links, wait_until


class SaveStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = Harness()
        self.queue = JobQueue(self.harness.factory, on_item_changed=self.harness.on_item_changed)

    def tearDown(self) -> None:
        for conversion in self.harness.started():
            conversion.release.set()
        self.queue.shutdown(wait=True, timeout=5)

    def finish(self, count: int, failed: int = 0) -> list[str]:
        ids = self.queue.add(links(count))
        self.assertTrue(wait_until(lambda: len(self.harness.started()) >= min(count, 2)))
        for conversion in self.harness.started()[:failed]:
            conversion.outcome = Phase.FAILED

        def idle() -> bool:
            for conversion in self.harness.started():
                conversion.release.set()
            return not self.queue.has_active() and self.queue.running_count == 0

        self.assertTrue(wait_until(idle))
        return ids

    def test_done_items_are_ready_until_saved(self):
        ids = self.finish(3)
        item = self.queue.get(ids[0])
        self.assertEqual(item.status, ItemStatus.READY)
        self.assertEqual(item.staged_path, Path(f"{ids[0]}-0.mp3"))
        self.assertIsNone(item.saved_path)
        self.assertTrue(item.can_save)
        self.assertEqual(len(self.queue.unsaved()), 3)

        self.harness.changes.clear()
        self.assertTrue(self.queue.mark_saved(ids[0], Path("D:/Music/Song.mp3")))
        item = self.queue.get(ids[0])
        self.assertEqual(item.status, ItemStatus.SAVED)
        self.assertEqual(item.saved_path, Path("D:/Music/Song.mp3"))
        self.assertEqual(item.output_path, Path("D:/Music/Song.mp3"))
        self.assertFalse(item.can_save)
        self.assertEqual([change.id for change in self.harness.changes], [ids[0]])

        counts = self.queue.status_counts()
        self.assertEqual(counts[ItemStatus.READY], 2)
        self.assertEqual(counts[ItemStatus.SAVED], 1)
        self.assertEqual(self.queue.counts()[Phase.DONE], 3)
        self.assertEqual({item.id for item in self.queue.unsaved()}, set(ids[1:]))

    def test_mark_saved_only_for_done_items(self):
        ids = self.queue.add(links(3))
        self.assertFalse(self.queue.mark_saved(ids[2], Path("x.mp3")))  # waiting
        self.assertFalse(self.queue.mark_saved("unknown", Path("x.mp3")))

    def test_failed_save_keeps_item_ready(self):
        [item_id] = self.finish(1)
        error = classify_error("[Errno 28] No space left on device")
        self.assertTrue(self.queue.mark_save_failed(item_id, error))
        item = self.queue.get(item_id)
        self.assertEqual(item.status, ItemStatus.READY)
        self.assertEqual(item.save_error, error)
        self.assertTrue(item.can_save)
        self.assertFalse(item.can_retry)
        self.assertFalse(self.queue.retry(item_id))
        self.queue.mark_saved(item_id, Path("a.mp3"))  # a later save clears the error
        self.assertIsNone(self.queue.get(item_id).save_error)

    def test_missing_staged_file_offers_retry(self):
        [item_id] = self.finish(1)
        self.queue.mark_save_failed(item_id, missing_file_error(), staged_missing=True)
        item = self.queue.get(item_id)
        self.assertFalse(item.can_save)
        self.assertTrue(item.can_retry)
        self.assertEqual(self.queue.unsaved(), [])
        self.assertTrue(self.queue.retry(item_id))
        item = self.queue.get(item_id)
        self.assertEqual(item.phase, Phase.WAITING)
        self.assertIsNone(item.staged_path)
        self.assertIsNone(item.save_error)
        self.assertFalse(item.staged_missing)

    def test_status_counts(self):
        ids = self.finish(4, failed=1)
        self.queue.mark_saved(ids[1], Path("b.mp3"))
        counts = self.queue.status_counts()
        self.assertEqual(
            {status: n for status, n in counts.items() if n},
            {ItemStatus.FAILED: 1, ItemStatus.SAVED: 1, ItemStatus.READY: 2},
        )

    def test_clear_finished_keep(self):
        ids = self.finish(3)
        removed = self.queue.clear_finished(keep=[ids[1]])
        self.assertEqual(set(removed), {ids[0], ids[2]})
        self.assertEqual([item.id for item in self.queue.snapshot()], [ids[1]])


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ready = self.root / "ready"
        ffmpeg = self.root / "ffmpeg.exe"
        ffmpeg.write_bytes(b"")
        fake = Path(__file__).with_name("fake_ytdlp.py")
        self.config = ConverterConfig(
            output_dir=self.root / "SHOULD-NOT-BE-USED",
            temp_root=self.root / "tmp",
            ytdlp=None,
            ffmpeg=ffmpeg,
            command_prefix=(sys.executable, str(fake), "success", "Same Title"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_each_job_gets_its_own_staging_folder(self):
        queue = JobQueue(
            factory_for(lambda: self.config, staging_root=self.ready), staging_root=self.ready
        )
        ids = queue.add(links(2))
        self.assertTrue(wait_until(lambda: queue.status_counts()[ItemStatus.READY] == 2, 20))
        queue.shutdown(wait=True, timeout=10)
        staged = [queue.get(item_id).staged_path for item_id in ids]
        self.assertEqual([path.name for path in staged], ["Same Title.mp3", "Same Title.mp3"])
        self.assertEqual({path.parent.parent for path in staged}, {self.ready})
        self.assertNotEqual(staged[0].parent, staged[1].parent)
        self.assertFalse((self.root / "SHOULD-NOT-BE-USED").exists())

    def test_result_after_stop_is_discarded_inside_staging_only(self):
        staged = self.ready / "job-x" / "Late.mp3"
        outside = self.root / "Downloads" / "Keep.mp3"
        cases = ((staged, self.ready, False), (outside, self.ready, True), (outside, None, True))
        for target, staging_root, should_exist in cases:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
            gate = threading.Event()

            class LateFinisher:
                def __init__(self, *args) -> None:
                    pass

                def run(self, target=target, gate=gate):
                    gate.wait(5)
                    return ConversionResult(Phase.DONE, title="Late", output_path=target)

                def stop(self) -> None:
                    pass  # finishes anyway, like a process that was already done

            queue = JobQueue(LateFinisher, staging_root=staging_root)
            [item_id] = queue.add(links(1))
            self.assertTrue(wait_until(lambda: queue.running_count == 1))
            queue.stop(item_id)
            gate.set()
            self.assertTrue(queue.join(5))
            with self.subTest(target=target.name, staging_root=staging_root):
                self.assertEqual(target.exists(), should_exist)
                self.assertEqual(queue.get(item_id).phase, Phase.STOPPED)


if __name__ == "__main__":
    unittest.main()

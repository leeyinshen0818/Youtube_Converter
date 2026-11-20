import random
import threading
import time
import unittest
from pathlib import Path

from core.converter import ConversionCallbacks, ConversionResult, Phase
from core.errors import classify_error
from core.jobqueue import JobQueue
from core.urls import parse_links


def links(count: int, start: int = 0):
    text = "\n".join(f"https://youtu.be/vid{index:08d}" for index in range(start, start + count))
    return parse_links(text).valid


def wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeConversion:
    """Blocks in run() until released or stopped."""

    def __init__(self, harness: "Harness", job_id: str, url: str, callbacks: ConversionCallbacks):
        self.harness = harness
        self.job_id = job_id
        self.url = url
        self.callbacks = callbacks
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.outcome = Phase.DONE

    def run(self) -> ConversionResult:
        harness = self.harness
        with harness.lock:
            harness.running += 1
            harness.max_running = max(harness.max_running, harness.running)
        try:
            self.callbacks.on_phase(Phase.DOWNLOADING)
            self.callbacks.on_title(f"Title {self.url[-4:]}")
            self.callbacks.on_progress(40.0)
            while not (self.release.is_set() or self.stopped.is_set()):
                time.sleep(0.005)
            if self.stopped.is_set():
                return ConversionResult(Phase.STOPPED)
            if self.outcome is Phase.FAILED:
                return ConversionResult(Phase.FAILED, error=classify_error("Video unavailable"))
            self.callbacks.on_phase(Phase.CONVERTING)
            return ConversionResult(
                Phase.DONE, title=f"Title {self.url[-4:]}", output_path=Path(f"{self.job_id}.mp3")
            )
        finally:
            with harness.lock:
                harness.running -= 1

    def stop(self) -> None:
        self.stopped.set()


class Harness:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.conversions: list[FakeConversion] = []
        self.changes = []
        self.count_events = 0
        self.removed = []

    def factory(self, job_id, url, video_id, callbacks):
        conversion = FakeConversion(self, job_id, url, callbacks)
        with self.lock:
            self.conversions.append(conversion)
        return conversion

    def started(self) -> list[FakeConversion]:
        with self.lock:
            return list(self.conversions)

    def on_item_changed(self, snapshot) -> None:
        with self.lock:
            self.changes.append(snapshot)

    def on_counts_changed(self) -> None:
        with self.lock:
            self.count_events += 1

    def on_removed(self, ids) -> None:
        with self.lock:
            self.removed.extend(ids)


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = Harness()
        self.clock_value = 1000.0
        self.queue = JobQueue(
            self.harness.factory,
            on_item_changed=self.harness.on_item_changed,
            on_counts_changed=self.harness.on_counts_changed,
            on_items_removed=self.harness.on_removed,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        for conversion in self.harness.started():
            conversion.release.set()
        self.queue.shutdown(wait=True, timeout=5)

    def clock(self) -> float:
        self.clock_value += 1
        return self.clock_value

    def phase(self, item_id: str) -> Phase:
        return self.queue.get(item_id).phase

    def release_all(self) -> None:
        for conversion in self.harness.started():
            conversion.release.set()

    def wait_started(self, count: int) -> None:
        self.assertTrue(wait_until(lambda: len(self.harness.started()) >= count))

    def wait_idle(self, release: bool = True) -> None:
        """Wait until nothing is active, releasing conversions as they start."""

        def idle() -> bool:
            if release:
                self.release_all()
            return not self.queue.has_active() and self.queue.running_count == 0

        self.assertTrue(wait_until(idle))

    def test_add_returns_waiting_items(self):
        ids = self.queue.add(links(3))
        self.assertEqual(len(ids), 3)
        snapshot = self.queue.snapshot()
        self.assertEqual([item.id for item in snapshot], ids)
        self.assertEqual(snapshot[0].url, "https://www.youtube.com/watch?v=vid00000000")
        self.assertEqual(snapshot[0].video_id, "vid00000000")
        self.assertEqual(self.queue.add([]), [])

    def test_concurrency_limit_of_two(self):
        ids = self.queue.add(links(6))
        self.wait_started(2)
        time.sleep(0.1)
        self.assertEqual(len(self.harness.started()), 2)
        counts = self.queue.counts()
        self.assertEqual(counts[Phase.WAITING], 4)
        self.assertEqual(counts[Phase.DOWNLOADING], 2)

        for _ in range(3):
            for conversion in self.harness.started():
                conversion.release.set()
            time.sleep(0.05)
        self.release_all()
        self.assertTrue(wait_until(lambda: self.queue.counts()[Phase.DONE] == 6))
        self.release_all()
        self.assertEqual(self.harness.max_running, 2)
        self.assertEqual({self.phase(item_id) for item_id in ids}, {Phase.DONE})

    def test_runs_in_input_order(self):
        self.queue.add(links(4))
        self.wait_started(2)
        self.assertEqual(
            [conversion.url[-4:] for conversion in self.harness.started()], ["0000", "0001"]
        )

    def test_progress_title_and_result_are_tracked(self):
        [item_id] = self.queue.add(links(1))
        self.wait_started(1)
        self.assertTrue(wait_until(lambda: self.queue.get(item_id).percent == 40.0))
        item = self.queue.get(item_id)
        self.assertEqual(item.title, "Title 0000")
        self.assertEqual(item.phase, Phase.DOWNLOADING)
        self.release_all()
        self.wait_idle()
        item = self.queue.get(item_id)
        self.assertEqual(item.phase, Phase.DONE)
        self.assertIsNone(item.percent)
        self.assertEqual(item.output_path, Path(f"{item_id}-0.mp3"))

    def test_failure_keeps_friendly_error(self):
        [item_id] = self.queue.add(links(1))
        self.wait_started(1)
        self.harness.started()[0].outcome = Phase.FAILED
        self.release_all()
        self.wait_idle()
        item = self.queue.get(item_id)
        self.assertEqual(item.phase, Phase.FAILED)
        self.assertEqual(item.error.message, "This video can't be opened")

    def test_stop_waiting_item_never_starts(self):
        ids = self.queue.add(links(3))
        self.wait_started(2)
        self.assertTrue(self.queue.stop(ids[2]))
        self.assertEqual(self.phase(ids[2]), Phase.STOPPED)
        self.release_all()
        self.assertTrue(wait_until(lambda: self.queue.counts()[Phase.DONE] == 2))
        time.sleep(0.05)
        self.assertEqual(len(self.harness.started()), 2)
        self.assertEqual(self.phase(ids[2]), Phase.STOPPED)

    def test_stop_running_item(self):
        ids = self.queue.add(links(3))
        self.wait_started(2)
        self.assertTrue(self.queue.stop(ids[0]))
        self.assertEqual(self.phase(ids[0]), Phase.STOPPED)
        # The kill runs on a background thread.
        self.assertTrue(self.harness.started()[0].stopped.wait(5))
        # The freed slot goes to the third item.
        self.wait_started(3)
        self.assertEqual(self.phase(ids[0]), Phase.STOPPED)
        self.assertFalse(self.queue.stop(ids[0]))

    def test_stop_all(self):
        ids = self.queue.add(links(5))
        self.wait_started(2)
        self.assertEqual(self.queue.stop_all(), 5)
        self.assertEqual({self.phase(item_id) for item_id in ids}, {Phase.STOPPED})
        self.assertTrue(wait_until(lambda: self.queue.running_count == 0))
        self.assertEqual(len(self.harness.started()), 2)

    def test_retry_failed_and_stopped(self):
        ids = self.queue.add(links(2))
        self.wait_started(2)
        self.harness.started()[0].outcome = Phase.FAILED
        self.queue.stop(ids[1])
        self.release_all()
        self.wait_idle()
        self.assertEqual(self.phase(ids[0]), Phase.FAILED)
        self.assertEqual(self.phase(ids[1]), Phase.STOPPED)

        self.assertTrue(self.queue.retry(ids[0]))
        self.assertTrue(self.queue.retry(ids[1]))
        self.assertIsNone(self.queue.get(ids[0]).error)
        self.wait_started(4)
        self.assertEqual(
            [conversion.job_id for conversion in self.harness.started()[2:]],
            [f"{ids[0]}-1", f"{ids[1]}-1"],
        )
        self.release_all()
        self.wait_idle()
        self.assertEqual({self.phase(item_id) for item_id in ids}, {Phase.DONE})

    def test_retry_rejected_for_active_or_done(self):
        ids = self.queue.add(links(3))
        self.wait_started(2)
        self.assertFalse(self.queue.retry(ids[0]))  # running
        self.assertFalse(self.queue.retry(ids[2]))  # waiting
        self.release_all()
        self.wait_idle()
        self.release_all()
        self.wait_idle()
        self.assertFalse(self.queue.retry(ids[0]))  # done
        self.assertFalse(self.queue.retry("unknown"))

    def test_stale_result_after_stop_and_retry_is_ignored(self):
        [item_id] = self.queue.add(links(1))
        self.wait_started(1)
        first = self.harness.started()[0]
        first.stop = lambda: None  # simulate a process that is slow to die
        self.queue.stop(item_id)
        self.assertTrue(self.queue.retry(item_id))
        # The old run still holds one slot; the retry takes the second one.
        self.wait_started(2)
        first.callbacks.on_progress(99.0)  # stale callback
        first.release.set()
        self.assertNotEqual(self.queue.get(item_id).percent, 99.0)
        self.release_all()
        self.wait_idle()
        self.assertEqual(self.queue.get(item_id).output_path, Path(f"{item_id}-1.mp3"))

    def test_clear_finished_keeps_active(self):
        ids = self.queue.add(links(4))
        self.wait_started(2)
        self.harness.started()[1].outcome = Phase.FAILED
        self.queue.stop(ids[3])
        self.harness.started()[0].release.set()
        self.harness.started()[1].release.set()
        self.assertTrue(wait_until(
            lambda: self.phase(ids[0]) == Phase.DONE and self.phase(ids[1]) == Phase.FAILED
        ))
        self.wait_started(3)

        removed = self.queue.clear_finished()
        self.assertEqual(set(removed), {ids[0], ids[1], ids[3]})
        self.assertEqual(set(self.harness.removed), set(removed))
        self.assertEqual([item.id for item in self.queue.snapshot()], [ids[2]])
        self.assertTrue(self.queue.get(ids[2]).phase.is_active)
        self.assertEqual(self.queue.clear_finished(), [])

    def test_newest_batch_first(self):
        old = self.queue.add(links(2, start=0))
        new = self.queue.add(links(2, start=10))
        snapshot = self.queue.snapshot()
        self.assertEqual([item.id for item in snapshot], new + old)
        self.assertEqual(len({item.batch_id for item in snapshot}), 2)
        self.assertGreater(snapshot[0].batch_created, snapshot[-1].batch_created)

    def test_callbacks_fire(self):
        [item_id] = self.queue.add(links(1))
        self.wait_started(1)
        self.release_all()
        self.wait_idle()
        phases = [change.phase for change in self.harness.changes if change.id == item_id]
        self.assertEqual(phases[0], Phase.WAITING)
        self.assertIn(Phase.DOWNLOADING, phases)
        self.assertIn(Phase.CONVERTING, phases)
        self.assertEqual(phases[-1], Phase.DONE)
        self.assertGreaterEqual(self.harness.count_events, 3)

    def test_callback_may_read_queue(self):
        seen = []
        queue = JobQueue(
            self.harness.factory,
            on_item_changed=lambda item: seen.append(queue.counts()),
            on_counts_changed=lambda: queue.snapshot(),
        )
        queue.add(links(1))
        self.wait_started(1)
        self.release_all()
        self.assertTrue(wait_until(lambda: not queue.has_active()))
        queue.shutdown(timeout=5)
        self.assertTrue(seen)

    def test_shutdown_refuses_new_work(self):
        self.queue.add(links(3))
        self.wait_started(2)
        self.queue.shutdown(wait=True, timeout=5)
        self.assertEqual(self.queue.running_count, 0)
        with self.assertRaises(RuntimeError):
            self.queue.add(links(1))

    def make_slow_to_stop(self, conversion: FakeConversion, seconds: float) -> threading.Event:
        """Make stop() take ``seconds`` (like a slow taskkill)."""
        called = threading.Event()
        original = conversion.stop

        def slow_stop() -> None:
            called.set()
            time.sleep(seconds)
            original()

        conversion.stop = slow_stop
        return called

    def test_stop_does_not_block_the_caller(self):
        [item_id] = self.queue.add(links(1))
        self.wait_started(1)
        conversion = self.harness.started()[0]
        called = self.make_slow_to_stop(conversion, 1.0)
        started = time.monotonic()
        self.assertTrue(self.queue.stop(item_id))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(self.phase(item_id), Phase.STOPPED)  # marked at once
        self.assertTrue(called.wait(2))
        self.assertTrue(conversion.stopped.wait(3))
        self.assertTrue(self.queue.join(5))
        self.assertTrue(self.queue.is_idle())

    def test_stop_all_does_not_block_the_caller(self):
        ids = self.queue.add(links(3))
        self.wait_started(2)
        for conversion in self.harness.started():
            self.make_slow_to_stop(conversion, 1.0)
        started = time.monotonic()
        self.assertEqual(self.queue.stop_all(), 3)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual({self.phase(item_id) for item_id in ids}, {Phase.STOPPED})
        self.assertFalse(self.queue.is_idle())
        self.assertTrue(self.queue.join(5))
        self.assertTrue(all(c.stopped.is_set() for c in self.harness.started()))
        self.assertEqual(len(self.harness.started()), 2)

    def test_shutdown_returns_at_once_and_join_waits(self):
        self.queue.add(links(2))
        self.wait_started(2)
        for conversion in self.harness.started():
            self.make_slow_to_stop(conversion, 1.0)
        started = time.monotonic()
        self.queue.shutdown()
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertFalse(self.queue.join(0.1))  # still killing
        self.assertTrue(self.queue.join(5))
        self.assertEqual(self.queue.running_count, 0)

    def test_thread_safety_smoke(self):
        """Hammer the queue from several threads; it must stay consistent."""
        errors = []
        all_ids: list[str] = []
        ids_lock = threading.Lock()

        def adder(seed: int) -> None:
            try:
                for round_ in range(5):
                    new = self.queue.add(links(3, start=seed * 100 + round_ * 3))
                    with ids_lock:
                        all_ids.extend(new)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def chaos(seed: int) -> None:
            rng = random.Random(seed)
            try:
                for _ in range(200):
                    with ids_lock:
                        target = rng.choice(all_ids) if all_ids else None
                    action = rng.random()
                    if target and action < 0.3:
                        self.queue.stop(target)
                    elif target and action < 0.5:
                        self.queue.retry(target)
                    elif action < 0.55:
                        self.queue.clear_finished()
                    else:
                        self.queue.snapshot()
                        self.queue.counts()
                    for conversion in self.harness.started():
                        if rng.random() < 0.3:
                            conversion.release.set()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=adder, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=chaos, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])

        # Drain: release everything until nothing is active.
        def drained() -> bool:
            self.release_all()
            return not self.queue.has_active() and self.queue.running_count == 0

        self.assertTrue(wait_until(drained, timeout=15))
        self.assertLessEqual(self.harness.max_running, 2)
        counts = self.queue.counts()
        self.assertEqual(sum(counts.values()), len(self.queue.snapshot()))
        self.assertEqual(counts[Phase.WAITING] + counts[Phase.DOWNLOADING] + counts[Phase.CONVERTING], 0)


if __name__ == "__main__":
    unittest.main()


class RealConversionQueueTests(unittest.TestCase):
    """Queue + real Conversion running the fake yt-dlp (no network)."""

    def setUp(self) -> None:
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.temp_root = root / "tmp"
        ffmpeg = root / "ffmpeg.exe"
        ffmpeg.write_bytes(b"")
        self.pid_file = root / "pids.json"
        fake = Path(__file__).with_name("fake_ytdlp.py")
        from core.converter import ConverterConfig
        from core.jobqueue import factory_for
        import sys

        config = ConverterConfig(
            output_dir=root / "out",
            temp_root=self.temp_root,
            ytdlp=None,
            ffmpeg=ffmpeg,
            command_prefix=(sys.executable, str(fake), "sleep_download", "Song"),
        )
        self.queue = JobQueue(factory_for(lambda: config))

    def tearDown(self) -> None:
        self.queue.shutdown(wait=True, timeout=10)
        self.temp.cleanup()

    def test_stop_returns_fast_and_cleans_up(self):
        import json
        import os
        from unittest.mock import patch

        from tests.test_converter import process_alive

        with patch.dict(os.environ, {"FAKE_YTDLP_PIDS": str(self.pid_file)}):
            [item_id] = self.queue.add(links(1))
            self.assertTrue(wait_until(
                lambda: self.pid_file.exists()
                and self.queue.get(item_id).phase is Phase.DOWNLOADING, 15
            ))
        pids = json.loads(self.pid_file.read_text())
        self.assertTrue(any(self.temp_root.iterdir()))

        started = time.monotonic()
        self.assertTrue(self.queue.stop(item_id))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(self.queue.get(item_id).phase, Phase.STOPPED)

        self.assertTrue(self.queue.join(15))
        self.assertEqual(list(self.temp_root.iterdir()), [])
        for pid in pids:
            self.assertTrue(wait_until(lambda: not process_alive(pid), 5), f"pid {pid} alive")
        self.assertEqual(self.queue.get(item_id).phase, Phase.STOPPED)

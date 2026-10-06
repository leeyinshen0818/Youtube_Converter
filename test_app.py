import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import app


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))


class QueueManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.temp_dir.name)
        self.download_patch = patch.object(app, "DOWNLOAD_DIR", self.download_dir)
        self.download_patch.start()
        with app.jobs_lock:
            app.jobs.clear()
            app.cancel_events.clear()

    def tearDown(self) -> None:
        with app.jobs_lock:
            app.jobs.clear()
            app.cancel_events.clear()
        self.download_patch.stop()
        self.temp_dir.cleanup()

    def add_job(self, batch_id, statuses):
        items = [
            app.ConversionItem(id=f"item-{index}", url=f"https://youtu.be/{index}", status=status)
            for index, status in enumerate(statuses)
        ]
        job = app.BatchJob(id=batch_id, created_at=time.time(), items=items)
        with app.jobs_lock:
            app.jobs[batch_id] = job
        return job

    def add_mp3(self, batch_id, item, filename, content=b"test mp3"):
        item.status = "done"
        item.files = [filename]
        target = self.download_dir / batch_id / item.id / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def test_clear_history_preserves_active_items(self):
        job = self.add_job("batch-a", ["done", "error", "running", "queued"])

        result = app.clear_job_items({"done", "error"})

        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["preservedActive"], 2)
        self.assertEqual([item.status for item in job.items], ["running", "queued"])

    def test_clear_completed_keeps_failed_active_and_files(self):
        job = self.add_job("batch-a", ["done", "error", "running", "queued"])
        saved_file = self.add_mp3("batch-a", job.items[0], "saved.mp3")

        result = app.clear_job_items({"done"})

        self.assertEqual(result["removed"], 1)
        self.assertEqual([item.status for item in job.items], ["error", "running", "queued"])
        self.assertTrue(saved_file.is_file())

    def test_stop_queued_item_does_not_start_conversion(self):
        job = self.add_job("older", ["queued"])
        app.cancel_events[("older", "item-0")] = threading.Event()

        result = app.stop_item("older", "item-0")
        app.run_conversion("older", "item-0", job.items[0].url)

        self.assertEqual(result["items"][0]["status"], "stopped")
        self.assertEqual(job.items[0].status, "stopped")
        self.assertIsNone(job.items[0].started_at)

    def test_running_item_stops_on_progress_callback(self):
        job = self.add_job("older", ["queued"])
        app.cancel_events[("older", "item-0")] = threading.Event()
        started = threading.Event()

        class FakeDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download):
                started.set()
                deadline = time.monotonic() + 3
                while not app.cancel_events[("older", "item-0")].is_set():
                    if time.monotonic() > deadline:
                        raise AssertionError("Cancellation was not requested")
                    time.sleep(0.01)
                self.options["progress_hooks"][0]({"status": "downloading"})

        with patch.object(app, "ffmpeg_location", return_value="ffmpeg"), patch(
            "yt_dlp.YoutubeDL", FakeDownloader
        ):
            worker = threading.Thread(
                target=app.run_conversion, args=("older", "item-0", job.items[0].url)
            )
            worker.start()
            self.assertTrue(started.wait(3))
            pending = app.stop_item("older", "item-0")
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(pending["items"][0]["status"], "stopping")
        self.assertEqual(job.items[0].status, "stopped")

    def test_missing_file_is_reported_and_skipped_by_zip(self):
        job = self.add_job("batch-a", ["done", "done"])
        job.items[0].files = ["missing.mp3"]
        self.add_mp3("batch-a", job.items[1], "available.mp3")

        snapshot = app.snapshot_job(job)
        zip_path, included = app.build_zip("batch-a")

        self.assertEqual(snapshot["items"][0]["downloadUrls"], [])
        self.assertEqual(snapshot["items"][0]["missingFiles"], ["missing.mp3"])
        self.assertEqual(included, [("batch-a", "item-1", "available.mp3")])
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.namelist(), ["available.mp3"])

    def test_retry_from_older_batch_does_not_change_newer_batch(self):
        older = self.add_job("older", ["error"])
        newer = self.add_job("newer", ["done"])
        fake_executor = FakeExecutor()

        with patch.object(app, "executor", fake_executor):
            result = app.retry_item("older", "item-0")

        self.assertEqual(result["items"][0]["status"], "queued")
        self.assertEqual(newer.items[0].status, "done")
        self.assertEqual(len(fake_executor.calls), 1)


class HttpQueueTests(QueueManagementTests):
    def setUp(self) -> None:
        super().setUp()
        self.server = app.ThreadingHTTPServer((app.SERVER_HOST, 0), app.AppHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://{app.SERVER_HOST}:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        super().tearDown()

    def request_json(self, path, method="GET"):
        request = urllib.request.Request(self.base_url + path, method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def wait_for_download_mark(self, item):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not item.downloaded_at:
            time.sleep(0.01)

    def test_individual_downloads_work_across_multiple_batches(self):
        older = self.add_job("older", ["done"])
        newer = self.add_job("newer", ["done"])
        self.add_mp3("older", older.items[0], "older.mp3", b"older")
        self.add_mp3("newer", newer.items[0], "newer.mp3", b"newer")

        with urllib.request.urlopen(
            self.base_url + "/api/download/older/item-0/older.mp3", timeout=5
        ) as response:
            self.assertEqual(response.read(), b"older")
        self.wait_for_download_mark(older.items[0])
        self.assertEqual(older.items[0].downloaded_files, ["older.mp3"])

        with urllib.request.urlopen(
            self.base_url + "/api/download/newer/item-0/newer.mp3", timeout=5
        ) as response:
            self.assertEqual(response.read(), b"newer")
        self.wait_for_download_mark(newer.items[0])
        self.assertEqual(newer.items[0].downloaded_files, ["newer.mp3"])

    def test_clear_history_endpoint_keeps_running_item(self):
        self.add_job("batch-a", ["done", "error", "running"])

        data = self.request_json("/api/jobs/clear-history", method="POST")

        self.assertEqual(data["removed"], 2)
        self.assertEqual(data["preservedActive"], 1)
        self.assertEqual(data["jobs"][0]["items"][0]["status"], "running")

    def test_clear_completed_endpoint_keeps_other_statuses(self):
        self.add_job("batch-a", ["done", "error", "running", "stopped"])

        data = self.request_json("/api/jobs/clear-completed", method="POST")

        self.assertEqual(data["removed"], 1)
        self.assertEqual(
            [item["status"] for item in data["jobs"][0]["items"]],
            ["error", "running", "stopped"],
        )

    def test_stop_endpoint_keeps_other_batch(self):
        self.add_job("older", ["queued"])
        self.add_job("newer", ["done"])
        app.cancel_events[("older", "item-0")] = threading.Event()

        data = self.request_json("/api/jobs/older/items/item-0/stop", method="POST")

        self.assertEqual(data["items"][0]["status"], "stopped")
        self.assertEqual(self.request_json("/api/jobs")["jobs"][1]["items"][0]["status"], "done")

    def test_missing_download_returns_not_found(self):
        self.add_job("batch-a", ["done"])

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                self.base_url + "/api/download/batch-a/item-0/missing.mp3", timeout=5
            )

        self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()

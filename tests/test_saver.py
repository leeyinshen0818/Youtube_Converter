import errno
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import saver
from core.errors import ErrorKind
from core.saver import (
    claim_name,
    default_save_dir,
    discard_staged,
    save_error,
    save_item,
)


class SaverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ready = root / "ready"
        self.dest = root / "Downloads" / "YouTube MP3 Converter"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def stage(self, name: str, job: str = "job1", content: bytes = b"mp3 data") -> Path:
        path = self.ready / job / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_default_save_dir(self):
        self.assertEqual(default_save_dir(), Path.home() / "Downloads" / "YouTube MP3 Converter")

    def test_moves_into_new_folder(self):
        staged = self.stage("My Song.mp3")
        saved = save_item(staged, self.dest)
        self.assertEqual(saved, self.dest / "My Song.mp3")
        self.assertEqual(saved.read_bytes(), b"mp3 data")
        self.assertFalse(staged.exists())

    def test_name_clash_suffixes(self):
        self.dest.mkdir(parents=True)
        (self.dest / "My Song.mp3").write_bytes(b"old")
        first = save_item(self.stage("My Song.mp3", "a", b"one"), self.dest)
        second = save_item(self.stage("My Song.mp3", "b", b"two"), self.dest)
        self.assertEqual(first.name, "My Song (2).mp3")
        self.assertEqual(second.name, "My Song (3).mp3")
        self.assertEqual((self.dest / "My Song.mp3").read_bytes(), b"old")
        self.assertEqual(second.read_bytes(), b"two")

    def test_unsafe_name_is_sanitized(self):
        staged = self.ready / "job" / "a.mp3"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"x")
        renamed = staged.with_name("CON.mp3")
        staged.rename(renamed)
        self.assertEqual(save_item(renamed, self.dest).name, "CON_.mp3")

    def test_concurrent_saves_never_share_a_name(self):
        staged = [self.stage("Same.mp3", f"job{i}", str(i).encode()) for i in range(8)]
        results, errors = [], []

        def run(path):
            try:
                results.append(save_item(path, self.dest))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(path,)) for path in staged]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({path.name for path in results}), 8)
        self.assertEqual(sorted(p.read_bytes() for p in results), sorted(str(i).encode() for i in range(8)))

    def test_cross_drive_copies_then_deletes(self):
        staged = self.stage("Song.mp3", content=b"big file")

        def cross_device(source, target):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with patch.object(saver.os, "replace", cross_device):
            saved = save_item(staged, self.dest)
        self.assertEqual(saved.read_bytes(), b"big file")
        self.assertFalse(staged.exists())

    def test_windows_not_same_device_error_is_cross_drive(self):
        error = OSError(errno.EACCES, "x")
        error.winerror = 17
        self.assertTrue(saver.is_cross_device(error))
        self.assertFalse(saver.is_cross_device(OSError(errno.EACCES, "denied")))

    def test_copy_failure_keeps_staged_and_leaves_no_partial_file(self):
        staged = self.stage("Song.mp3")

        def cross_device(source, target):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        def disk_full(source, target):
            Path(target).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(saver.os, "replace", cross_device), \
                patch.object(saver.shutil, "copyfile", disk_full):
            with self.assertRaises(OSError) as ctx:
                save_item(staged, self.dest)
        self.assertTrue(staged.exists())
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(save_error(ctx.exception).kind, ErrorKind.DISK_FULL)

    def test_permission_failure_keeps_staged(self):
        staged = self.stage("Song.mp3")

        def denied(source, target):
            raise PermissionError(errno.EACCES, "Permission denied")

        with patch.object(saver.os, "replace", denied):
            with self.assertRaises(PermissionError) as ctx:
                save_item(staged, self.dest)
        self.assertTrue(staged.exists())
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(save_error(ctx.exception).kind, ErrorKind.NO_PERMISSION)

    def test_missing_staged_file(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            save_item(self.ready / "gone" / "x.mp3", self.dest)
        error = save_error(ctx.exception)
        self.assertEqual(error.kind, ErrorKind.FILE_MISSING)
        self.assertEqual(error.message, "File is no longer available")
        self.assertIn("Retry", error.hint)

    def test_unknown_save_error_is_about_saving(self):
        error = save_error(OSError("something odd"))
        self.assertEqual(error.message, "Couldn't save the MP3")
        self.assertIn("something odd", error.details)

    def test_claim_name_creates_the_file(self):
        claimed = claim_name(self.dest, "x", ".mp3")
        self.assertTrue(claimed.is_file())
        self.assertEqual(claim_name(self.dest, "x", ".mp3").name, "x (2).mp3")

    def test_discard_staged_removes_file_and_empty_job_folder(self):
        staged = self.stage("Song.mp3")
        discard_staged(staged)
        self.assertFalse(staged.parent.exists())
        self.assertTrue(self.ready.exists())
        discard_staged(staged)  # already gone: no error
        discard_staged(None)


if __name__ == "__main__":
    unittest.main()

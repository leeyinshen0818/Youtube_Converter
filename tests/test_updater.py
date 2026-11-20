"""core.updater with an in-memory opener, temp folders and fake yt-dlp
scripts run through sys.executable (no network, no real yt-dlp)."""

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from core import updater
from core.updater import Stage, UpdateKind, UpdateRequest
from tests.updater_support import (
    EXE_URL,
    SUMS_URL,
    TAG,
    FakeOpener,
    release_json,
    release_opener,
    run_with_python,
    script,
    sums_for,
    write_exe,
)

OLD = "2024.01.01"


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.tools = root / "appdata" / "tools"
        self.target = self.tools / "yt-dlp.exe"
        self.backup = self.tools / "yt-dlp.exe.bak"
        self.bundled = root / "app" / "yt-dlp.exe"
        self.new_exe = script(TAG)
        self.stages: list[Stage] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, opener, current: Path | None, **changes) -> UpdateRequest:
        def progress(stage, done, total):
            self.stages.append(stage)

        values = dict(
            tools_dir=self.tools,
            current_exe=current,
            opener=opener,
            command_for=run_with_python,
            progress=progress,
            version_timeout=30,
        )
        values.update(changes)
        return UpdateRequest(**values)

    def leftovers(self) -> list[str]:
        if not self.tools.exists():
            return []
        return sorted(p.name for p in self.tools.iterdir() if p.name not in ("yt-dlp.exe", "yt-dlp.exe.bak"))

    def assert_old_kept(self, result) -> None:
        self.assertEqual(result.kind, UpdateKind.FAILED)
        self.assertEqual(result.message, "Couldn't update. Your current version still works.")
        self.assertEqual(self.target.read_bytes(), script(OLD))
        self.assertEqual(updater.read_version(self.target, run_with_python), OLD)
        self.assertEqual(self.leftovers(), [])

    # --- outcomes ---------------------------------------------------------------

    def test_success_backs_up_and_replaces(self):
        write_exe(self.target, OLD)
        result = updater.update_ytdlp(self.request(release_opener(self.new_exe), self.target))
        self.assertEqual(result.kind, UpdateKind.UPDATED)
        self.assertEqual(result.version, TAG)
        self.assertEqual(self.target.read_bytes(), self.new_exe)
        self.assertEqual(self.backup.read_bytes(), script(OLD))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(
            [s for i, s in enumerate(self.stages) if i == 0 or s != self.stages[i - 1]],
            [Stage.CHECKING, Stage.DOWNLOADING, Stage.VERIFYING, Stage.INSTALLING],
        )

    def test_update_over_bundled_copy_installs_into_tools(self):
        write_exe(self.bundled, OLD)
        result = updater.update_ytdlp(self.request(release_opener(self.new_exe), self.bundled))
        self.assertEqual(result.kind, UpdateKind.UPDATED)
        self.assertEqual(self.target.read_bytes(), self.new_exe)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.bundled.read_bytes(), script(OLD))  # shipped copy untouched

    def test_already_latest(self):
        write_exe(self.target, TAG)
        opener = release_opener(self.new_exe)
        result = updater.update_ytdlp(self.request(opener, self.target))
        self.assertEqual(result.kind, UpdateKind.ALREADY_LATEST)
        self.assertEqual(result.version, TAG)
        self.assertNotIn(EXE_URL, opener.requested)
        self.assertFalse(self.backup.exists())

    def test_newer_nightly_counts_as_latest(self):
        write_exe(self.target, f"{TAG}.123456")
        result = updater.update_ytdlp(self.request(release_opener(self.new_exe), self.target))
        self.assertEqual(result.kind, UpdateKind.ALREADY_LATEST)

    def test_first_install(self):
        result = updater.update_ytdlp(self.request(release_opener(self.new_exe), None))
        self.assertEqual(result.kind, UpdateKind.INSTALLED)
        self.assertEqual(result.version, TAG)
        self.assertEqual(self.target.read_bytes(), self.new_exe)
        self.assertFalse(self.backup.exists())

    def test_first_install_failure_message(self):
        opener = FakeOpener({updater.LATEST_RELEASE_URL: urllib.error.URLError("getaddrinfo failed")})
        result = updater.update_ytdlp(self.request(opener, None))
        self.assertEqual(result.kind, UpdateKind.FAILED)
        self.assertEqual(result.message, updater.FAILED_INSTALL_TEXT)
        self.assertFalse(self.target.exists())

    def test_hash_mismatch(self):
        write_exe(self.target, OLD)
        opener = release_opener(self.new_exe, sums=sums_for(b"something else"))
        result = updater.update_ytdlp(self.request(opener, self.target))
        self.assert_old_kept(result)
        self.assertIn("SHA-256 mismatch", result.details)
        self.assertFalse(self.backup.exists())

    def test_missing_hash_entry(self):
        write_exe(self.target, OLD)
        opener = release_opener(self.new_exe, sums=sums_for(self.new_exe, name="yt-dlp_x86.exe"))
        self.assert_old_kept(updater.update_ytdlp(self.request(opener, self.target)))

    def test_bad_version_output(self):
        write_exe(self.target, OLD)
        for bad in (script("Traceback: broken"), script(TAG, exit_code=1), b"raise SystemExit(3)\n"):
            with self.subTest(bad=bad[:30]):
                result = updater.update_ytdlp(self.request(release_opener(bad), self.target))
                self.assert_old_kept(result)
                self.assertIn("did not report a version", result.details)

    def test_network_error(self):
        write_exe(self.target, OLD)
        opener = FakeOpener({updater.LATEST_RELEASE_URL: urllib.error.URLError("getaddrinfo failed")})
        result = updater.update_ytdlp(self.request(opener, self.target))
        self.assert_old_kept(result)
        self.assertIn("getaddrinfo failed", result.details)

    def test_network_error_during_download(self):
        write_exe(self.target, OLD)
        opener = release_opener(self.new_exe)
        opener.routes[EXE_URL] = ConnectionResetError(10054, "connection reset")
        self.assert_old_kept(updater.update_ytdlp(self.request(opener, self.target)))

    def test_http_error_and_bad_release_data(self):
        write_exe(self.target, OLD)
        cases = {
            "404": FakeOpener({}),
            "not json": FakeOpener({updater.LATEST_RELEASE_URL: b"<html>"}),
            "bad tag": FakeOpener({updater.LATEST_RELEASE_URL: release_json(tag="latest; rm -rf")}),
            "no assets": FakeOpener({updater.LATEST_RELEASE_URL: b'{"tag_name": "2025.10.22", "assets": []}'}),
        }
        for name, opener in cases.items():
            with self.subTest(name=name):
                self.assert_old_kept(updater.update_ytdlp(self.request(opener, self.target)))

    def test_download_size_must_match_release(self):
        write_exe(self.target, OLD)
        opener = release_opener(self.new_exe)
        opener.routes[updater.LATEST_RELEASE_URL] = release_json(size=len(self.new_exe) + 10)
        result = updater.update_ytdlp(self.request(opener, self.target))
        self.assert_old_kept(result)
        self.assertIn("incomplete", result.details)

    def test_cancel_mid_download(self):
        write_exe(self.target, OLD)
        big = script(TAG, padding=updater.CHUNK_BYTES * 4)
        cancel = threading.Event()
        seen = []

        def progress(stage, done, total):
            seen.append((stage, done))
            if stage is Stage.DOWNLOADING and done > 0:
                cancel.set()

        result = updater.update_ytdlp(self.request(release_opener(big), self.target, progress=progress, cancel=cancel))
        self.assertEqual(result.kind, UpdateKind.CANCELLED)
        self.assertEqual(self.target.read_bytes(), script(OLD))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.backup.exists())
        self.assertLess(max(done for stage, done in seen if stage is Stage.DOWNLOADING), len(big))

    def test_cancel_before_start(self):
        write_exe(self.target, OLD)
        cancel = threading.Event()
        cancel.set()
        opener = release_opener(self.new_exe)
        result = updater.update_ytdlp(self.request(opener, self.target, cancel=cancel))
        self.assertEqual(result.kind, UpdateKind.CANCELLED)
        self.assertNotIn(EXE_URL, opener.requested)

    def test_replace_failure_restores_backup(self):
        write_exe(self.target, OLD)

        def broken_replace(source, destination):
            Path(destination).unlink()  # worst case: the old file is already gone
            raise PermissionError(13, "Access is denied")

        result = updater.update_ytdlp(self.request(release_opener(self.new_exe), self.target, replace=broken_replace))
        self.assert_old_kept(result)
        self.assertIn("PermissionError", result.details)

    def test_replace_failure_with_locked_file(self):
        write_exe(self.target, OLD)

        def locked(source, destination):
            raise PermissionError(32, "The process cannot access the file because it is being used")

        result = updater.update_ytdlp(self.request(release_opener(self.new_exe), self.target, replace=locked))
        self.assert_old_kept(result)

    # --- URL safety --------------------------------------------------------------

    def test_allowed_urls(self):
        allowed = [
            "https://github.com/yt-dlp/yt-dlp/releases/latest",
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            "https://objects.githubusercontent.com/github-production-release-asset/x",
            "https://release-assets.githubusercontent.com/x",
        ]
        blocked = [
            "http://github.com/yt-dlp/yt-dlp",
            "https://evil.com/github.com",
            "https://github.com.evil.com/x",
            "https://githubusercontent.com/x",
            "https://evilgithubusercontent.com/x",
            "https://user:pw@github.com/x",
            "https://github.com:8443/x",
            "ftp://github.com/x",
            "file:///C:/Windows/system32/calc.exe",
        ]
        for url in allowed:
            self.assertTrue(updater.is_allowed_url(url), url)
        for url in blocked:
            self.assertFalse(updater.is_allowed_url(url), url)

    def test_only_official_release_assets(self):
        self.assertTrue(updater.is_official_asset_url(EXE_URL, "yt-dlp.exe"))
        for url in (
            "https://github.com/someone/yt-dlp/releases/download/2025.10.22/yt-dlp.exe",
            "https://github.com/yt-dlp/yt-dlp/releases/download/2025.10.22/other.exe",
            "https://github.com/yt-dlp/yt-dlp/releases/download/a/b/yt-dlp.exe",
            "http://github.com/yt-dlp/yt-dlp/releases/download/2025.10.22/yt-dlp.exe",
        ):
            self.assertFalse(updater.is_official_asset_url(url, "yt-dlp.exe"), url)

    def test_unofficial_asset_in_release_is_refused(self):
        write_exe(self.target, OLD)
        opener = release_opener(self.new_exe)
        evil = "https://github.com/someone/yt-dlp/releases/download/2025.10.22/yt-dlp.exe"
        opener.routes[updater.LATEST_RELEASE_URL] = release_json(exe_url=evil)
        opener.routes[evil] = self.new_exe
        self.assert_old_kept(updater.update_ytdlp(self.request(opener, self.target)))
        self.assertNotIn(evil, opener.requested)

    def test_redirect_to_other_host_is_refused(self):
        write_exe(self.target, OLD)
        opener = release_opener(self.new_exe)
        opener.final_urls[EXE_URL] = "https://downloads.example.com/yt-dlp.exe"
        result = updater.update_ytdlp(self.request(opener, self.target))
        self.assert_old_kept(result)
        self.assertIn("not allowed", result.details)

    def test_redirect_handler_blocks_bad_targets(self):
        handler = updater.SafeRedirectHandler()
        request = urllib.request.Request(EXE_URL)
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://github.com/x")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x")
        allowed = handler.redirect_request(
            request, None, 302, "Found", {}, "https://objects.githubusercontent.com/asset"
        )
        self.assertEqual(allowed.full_url, "https://objects.githubusercontent.com/asset")

    def test_default_opener_has_safe_redirects(self):
        opener = updater.default_opener()
        self.assertTrue(any(isinstance(h, updater.SafeRedirectHandler) for h in opener.handlers))

    # --- helpers ---------------------------------------------------------------

    def test_parse_sums(self):
        digest = "a" * 64
        text = f"{'b' * 64}  yt-dlp\n{digest} *yt-dlp.exe\n"
        self.assertEqual(updater.parse_sums(text), digest)
        self.assertIsNone(updater.parse_sums("nothing here"))
        self.assertIsNone(updater.parse_sums("xyz  yt-dlp.exe"))

    def test_read_version(self):
        exe = write_exe(self.tools / "fake.exe", TAG)
        self.assertEqual(updater.read_version(exe, run_with_python), TAG)
        self.assertIsNone(updater.read_version(self.tools / "missing.exe"))
        slow = self.tools / "slow.exe"
        slow.write_bytes(script(TAG, sleep=20))
        self.assertIsNone(updater.read_version(slow, run_with_python, timeout=0.5))

    def test_version_compare(self):
        self.assertTrue(updater.is_version("2025.10.22"))
        self.assertTrue(updater.is_version("2025.10.22.123456"))
        self.assertFalse(updater.is_version("v2025"))
        self.assertGreater(updater.version_key("2025.10.22"), updater.version_key("2025.9.30"))

    def test_tools_busy(self):
        self.assertFalse(updater.tools_busy(False, 0))
        self.assertTrue(updater.tools_busy(True, 0))
        self.assertTrue(updater.tools_busy(False, 1))  # a stopped job's process may still run


if __name__ == "__main__":
    unittest.main()

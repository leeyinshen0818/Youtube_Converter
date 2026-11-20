"""Build helpers (scripts/build_support.py) and packaging details.

The yt-dlp download uses the in-memory opener from updater_support: no
network, and the fake "exe" is a Python script.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from core.version import APP_VERSION
from scripts import build_support
from tests import updater_support as fake
from ui.app import icon_path

ROOT = Path(__file__).resolve().parents[1]


class VersionInfoTests(unittest.TestCase):
    def test_version_tuple(self):
        self.assertEqual(build_support.version_tuple("2.0.0"), (2, 0, 0, 0))
        self.assertEqual(build_support.version_tuple("2.0.0-dev"), (2, 0, 0, 0))
        self.assertEqual(build_support.version_tuple("3.1.4.5"), (3, 1, 4, 5))
        with self.assertRaises(ValueError):
            build_support.version_tuple("dev")

    def test_app_version_is_a_release(self):
        self.assertEqual(APP_VERSION, "2.0.0")

    def test_text_uses_app_version_and_product_name(self):
        text = build_support.version_info_text()
        self.assertIn(f"StringStruct('ProductVersion', '{APP_VERSION}')", text)
        self.assertIn(f"StringStruct('FileVersion', '{APP_VERSION}')", text)
        self.assertIn("StringStruct('ProductName', 'YouTube to MP3 Converter')", text)
        self.assertIn("filevers=(2, 0, 0, 0)", text)
        self.assertNotIn("Batch", text)

    def test_tracked_file_matches_app_version(self):
        tracked = (ROOT / "windows_version_info.txt").read_text(encoding="utf-8")
        self.assertEqual(tracked.replace("\r\n", "\n"), build_support.version_info_text())

    def test_window_title_reads_app_version(self):
        from ui.main_window import WINDOW_TITLE

        self.assertEqual(WINDOW_TITLE, f"YouTube to MP3 Converter {APP_VERSION}")


class FetchYtdlpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "ytdlp"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_main(self, opener) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_support.main(["ytdlp", str(self.folder)], opener, fake.run_with_python)
        return code, out.getvalue(), err.getvalue()

    def test_downloads_and_verifies(self):
        exe = fake.script(fake.TAG)
        code, out, _ = self.run_main(fake.release_opener(exe))
        self.assertEqual(code, 0)
        self.assertEqual((self.folder / "yt-dlp.exe").read_bytes(), exe)
        self.assertIn(fake.TAG, out)
        self.assertIn("SHA2-256SUMS", out)

    def test_old_copy_is_replaced_without_backup(self):
        fake.write_exe(self.folder / "yt-dlp.exe", "2099.01.01")
        exe = fake.script(fake.TAG)
        code, _, _ = self.run_main(fake.release_opener(exe))
        self.assertEqual(code, 0)
        self.assertEqual((self.folder / "yt-dlp.exe").read_bytes(), exe)
        self.assertFalse((self.folder / "yt-dlp.exe.bak").exists())

    def test_hash_mismatch_fails_loudly_and_leaves_nothing(self):
        fake.write_exe(self.folder / "yt-dlp.exe", "2001.01.01")  # stale copy must not survive
        exe = fake.script(fake.TAG)
        opener = fake.release_opener(exe, sums=fake.sums_for(b"something else"))
        code, out, err = self.run_main(opener)
        self.assertEqual(code, 1)
        self.assertIn("FAILED", err)
        self.assertIn("SHA-256 mismatch", err)
        self.assertEqual(out, "")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [])

    def test_unofficial_url_fails(self):
        exe = fake.script(fake.TAG)
        opener = fake.release_opener(exe)
        bad = "https://example.com/yt-dlp.exe"
        opener.routes[fake.updater.LATEST_RELEASE_URL] = fake.release_json(exe_url=bad, size=len(exe))
        code, _, err = self.run_main(opener)
        self.assertEqual(code, 1)
        self.assertIn("official", err)
        self.assertFalse((self.folder / "yt-dlp.exe").exists())

    def test_exe_that_reports_no_version_fails(self):
        exe = fake.script("not a version")
        code, _, err = self.run_main(fake.release_opener(exe))
        self.assertEqual(code, 1)
        self.assertIn("version", err)
        self.assertFalse((self.folder / "yt-dlp.exe").exists())


class ExeCheckTests(unittest.TestCase):
    GOOD = [
        "PYZ.pyz", "main", "bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/node.exe", "bin/yt-dlp.exe",
        "icon/app.ico", "PySide6/Qt6Core.dll",
    ]

    def test_complete_exe_passes(self):
        self.assertEqual(build_support.check_contents(self.GOOD), [])

    def test_missing_tool_fails(self):
        names = [n for n in self.GOOD if n != "bin/node.exe"]
        self.assertEqual(build_support.check_contents(names), ["missing bin/node.exe"])

    def test_private_files_fail(self):
        for leak in ("cookies.txt", "sub/Cookies.TXT", "settings.json"):
            with self.subTest(leak=leak):
                problems = build_support.check_contents([*self.GOOD, leak])
                self.assertEqual(problems, [f"private file inside: {leak}"])


class IconTests(unittest.TestCase):
    def test_window_icon_prefers_bundled_ico(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "icon").mkdir()
            self.assertEqual(icon_path(base), base / "icon" / "logo.png")
            (base / "icon" / "app.ico").write_bytes(b"")
            self.assertEqual(icon_path(base), base / "icon" / "app.ico")

    def test_ico_has_every_size_and_is_square(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is a build requirement")
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp) / "wide.png"
            Image.new("RGBA", (31, 30), (200, 0, 0, 255)).save(png)
            out = Path(temp) / "app.ico"
            sizes = build_support.write_icon(png, out)
            self.assertEqual([w for w, _ in sizes], list(build_support.ICON_SIZES))
            self.assertTrue(all(w == h for w, h in sizes))
            with Image.open(png) as source:
                square = build_support.square_image(source)
            self.assertEqual(square.size, (31, 31))
            self.assertEqual(square.getpixel((15, 30))[3], 0)  # padded, not stretched
            self.assertEqual(square.getpixel((15, 0))[3], 255)

    def test_invisible_halo_is_trimmed_but_never_visible_pixels(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is a build requirement")
        image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        image.putpixel((0, 0), (255, 255, 255, build_support.TRIM_ALPHA))  # faint halo pixel
        image.putpixel((99, 99), (255, 255, 255, build_support.TRIM_ALPHA + 1))  # visible
        image.paste((0, 0, 255, 255), (40, 10, 60, 90))
        square = build_support.square_image(image)
        self.assertEqual(square.size, (90, 90))  # (40,10)-(100,100) = 60 x 90 kept, padded
        self.assertEqual(square.getpixel((15 + 59, 89))[3], build_support.TRIM_ALPHA + 1)

    def test_icon_sheet_renders(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is a build requirement")
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp) / "logo.png"
            Image.new("RGBA", (64, 80), (200, 0, 0, 255)).save(png)
            ico = Path(temp) / "app.ico"
            build_support.write_icon(png, ico)
            out = Path(temp) / "sheet" / "icon_sheet.png"
            build_support.write_icon_sheet(ico, out)
            with Image.open(out) as sheet:
                self.assertGreater(sheet.width, 48 * len(build_support.SHEET_SIZES))
                self.assertEqual(sheet.getpixel((2, 2)), (243, 243, 243))
                self.assertEqual(sheet.getpixel((2, sheet.height - 2)), (32, 32, 32))

    def test_transparent_margins_are_trimmed_before_padding(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is a build requirement")
        image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        image.paste((0, 0, 255, 255), (40, 10, 60, 90))  # a 20 x 80 "can"
        square = build_support.square_image(image)
        self.assertEqual(square.size, (80, 80))
        self.assertEqual(square.getchannel("A").getbbox(), (30, 0, 50, 80))


if __name__ == "__main__":
    unittest.main()

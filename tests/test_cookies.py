"""core.cookies: status, import (validated), remove; contents never leak."""

import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path

from core import cookies
from core.cookies import CookieStatus

SECRET = "SECRET-SESSION-TOKEN-1234567890"
VALID = (
    "# Netscape HTTP Cookie File\n"
    "# This is a generated file! Do not edit.\n\n"
    f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t{SECRET}\n"
    f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\t{SECRET}\n"
)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


class CookieTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app_data = root / "appdata"
        self.app_dir = root / "app"
        self.app_dir.mkdir()
        self.source = root / "picked" / "cookies.txt"
        self.source.parent.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_status(self):
        self.assertEqual(cookies.cookie_status(self.app_data, self.app_dir), CookieStatus.NOT_IMPORTED)
        (self.app_dir / "cookies.txt").write_text(VALID)
        self.assertEqual(cookies.cookie_status(self.app_data, self.app_dir), CookieStatus.NEXT_TO_APP)
        self.source.write_text(VALID)
        self.assertTrue(cookies.import_cookies(self.source, self.app_data).ok)
        self.assertEqual(cookies.cookie_status(self.app_data, self.app_dir), CookieStatus.IMPORTED)

    def test_import_copies_to_app_data(self):
        self.source.write_text(VALID)
        result = cookies.import_cookies(self.source, self.app_data)
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Cookies imported.")
        self.assertEqual((self.app_data / "cookies.txt").read_text(), VALID)
        self.assertTrue(self.source.exists())  # the original is left alone
        self.assertEqual([p.name for p in self.app_data.iterdir()], ["cookies.txt"])

    def test_import_without_header_but_cookie_lines(self):
        self.source.write_text(f".youtube.com\tTRUE\t/\tFALSE\t0\tPREF\t{SECRET}\n")
        self.assertTrue(cookies.import_cookies(self.source, self.app_data).ok)

    def test_invalid_files_rejected(self):
        bad = {
            "empty": b"",
            "text": b"hello world\nthis is not a cookie file\n",
            "header only": b"# Netscape HTTP Cookie File\n",
            "json": b'[{"domain": ".youtube.com", "name": "SID"}]',
            "binary": bytes(range(256)) * 4,
            "wrong fields": b".youtube.com\tTRUE\t/\n",
            "mixed": f".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{SECRET}\nnot a cookie\n".encode(),
        }
        for name, data in bad.items():
            with self.subTest(name=name):
                self.source.write_bytes(data)
                result = cookies.import_cookies(self.source, self.app_data)
                self.assertFalse(result.ok)
                self.assertEqual(result.message, "This doesn't look like a cookies.txt file. Choose another one.")
                self.assertFalse((self.app_data / "cookies.txt").exists())

    def test_rejected_file_keeps_previous_import(self):
        self.source.write_text(VALID)
        cookies.import_cookies(self.source, self.app_data)
        other = self.source.with_name("other.txt")
        other.write_text("nope")
        self.assertFalse(cookies.import_cookies(other, self.app_data).ok)
        self.assertEqual((self.app_data / "cookies.txt").read_text(), VALID)

    def test_missing_or_huge_file(self):
        result = cookies.import_cookies(self.source.with_name("missing.txt"), self.app_data)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Couldn't read this file. Choose another one.")
        with open(self.source, "wb") as file:
            file.truncate(cookies.MAX_COOKIES_BYTES + 1)
        self.assertFalse(cookies.import_cookies(self.source, self.app_data).ok)

    def test_reimporting_the_imported_copy(self):
        self.source.write_text(VALID)
        cookies.import_cookies(self.source, self.app_data)
        self.assertTrue(cookies.import_cookies(self.app_data / "cookies.txt", self.app_data).ok)
        self.assertEqual((self.app_data / "cookies.txt").read_text(), VALID)

    def test_remove_deletes_only_app_data_copy(self):
        beside_app = self.app_dir / "cookies.txt"
        beside_app.write_text(VALID)
        self.source.write_text(VALID)
        cookies.import_cookies(self.source, self.app_data)
        result = cookies.remove_cookies(self.app_data)
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Cookies removed.")
        self.assertFalse((self.app_data / "cookies.txt").exists())
        self.assertTrue(beside_app.exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(cookies.cookie_status(self.app_data, self.app_dir), CookieStatus.NEXT_TO_APP)
        self.assertTrue(cookies.remove_cookies(self.app_data).ok)  # nothing left: still fine

    def test_contents_never_logged_or_printed(self):
        handler = _Capture()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        results = []
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.source.write_text(VALID)
                results.append(cookies.import_cookies(self.source, self.app_data))
                self.source.write_text(f"garbage {SECRET}\n")
                results.append(cookies.import_cookies(self.source, self.app_data))
                self.source.write_text(f".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{SECRET}\nbad {SECRET}\n")
                results.append(cookies.import_cookies(self.source, self.app_data))
                results.append(cookies.remove_cookies(self.app_data))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        everything = "\n".join([out.getvalue(), err.getvalue(), *handler.records,
                                *(r.message for r in results), *(repr(r) for r in results)])
        self.assertNotIn(SECRET, everything)
        self.assertEqual([r.ok for r in results], [True, False, False, True])


if __name__ == "__main__":
    unittest.main()

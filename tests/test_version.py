import re
import unittest

from core.version import APP_VERSION


class VersionTests(unittest.TestCase):
    def test_version_format(self):
        self.assertRegex(APP_VERSION, re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$"))
        self.assertTrue(APP_VERSION.startswith("2.0.0"))


if __name__ == "__main__":
    unittest.main()

"""Offscreen layout matrix: one subprocess per DPR (QT_SCALE_FACTOR is
process-wide and is set only inside tests/ui_layout_probe.py).

Sizes are logical client-area sizes. 910x512 is 1366x768 at 150 %;
910x440 is roughly the maximized client area there (taskbar + title bar).
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

PROBE = Path(__file__).with_name("ui_layout_probe.py")
DPRS = ("1.0", "1.25", "1.5")
SIZES = ("910x440", "910x512", "1366x768", "1536x864", "1920x1080")
SMALL_SIZES = {"910x440", "910x512"}


def run_probe(dpr: str) -> dict[str, dict]:
    completed = subprocess.run(
        [sys.executable, str(PROBE), dpr, *SIZES],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        cwd=PROBE.parents[1],
    )
    if completed.returncode != 0:
        raise AssertionError(f"probe failed for DPR {dpr}:\n{completed.stderr[-3000:]}")
    results = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    return {result["size"]: result for result in results}


class LayoutMatrixTests(unittest.TestCase):
    results: dict[str, dict[str, dict]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {dpr: run_probe(dpr) for dpr in DPRS}

    def test_every_size_was_measured(self):
        for dpr in DPRS:
            self.assertEqual(set(self.results[dpr]), set(SIZES), dpr)

    def test_device_pixel_ratio_applied(self):
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                self.assertAlmostEqual(result["dpr"], float(dpr), places=2, msg=size)

    def test_no_clipping_or_overlap(self):
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                with self.subTest(dpr=dpr, size=size):
                    self.assertEqual(result["problems"], [])

    def test_window_fits_the_size(self):
        """The layout's minimum size never forces the window past the screen."""
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                width, height = (int(value) for value in size.split("x"))
                with self.subTest(dpr=dpr, size=size):
                    self.assertEqual(result["window"], [width, height])
                    self.assertLessEqual(result["minimum"][0], width)
                    self.assertLessEqual(result["minimum"][1], height)

    def test_convert_always_fully_visible(self):
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                with self.subTest(dpr=dpr, size=size):
                    self.assertTrue(result["convert_visible"], result["convert"])
                    self.assertGreaterEqual(result["convert"][3], result["convert_min_height"])

    def test_small_screens_stay_usable(self):
        for dpr in DPRS:
            for size in SMALL_SIZES:
                result = self.results[dpr][size]
                with self.subTest(dpr=dpr, size=size):
                    self.assertGreaterEqual(result["input_lines"], 3)
                    self.assertGreaterEqual(result["queue_rows"], 3)
                    self.assertTrue(result["compact_notice"])

    def test_queue_header_fits(self):
        """Summary, Download All / Open Folder / Clear Finished and the
        "All done" line are shown at every size (wrapping when narrow)."""
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                with self.subTest(dpr=dpr, size=size):
                    self.assertTrue(result["header_buttons_visible"])
                    self.assertIn("1 ready", result["summary"])
                    self.assertIn("1 saved", result["summary"])

    def test_settings_and_update_entry_points_visible(self):
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                with self.subTest(dpr=dpr, size=size):
                    self.assertTrue(result["settings_button_visible"])
                    self.assertTrue(result["update_link_visible"])

    def test_dialogs_fit_inside_the_window(self):
        """Settings (Advanced open, long folder, both messages) and Update
        (downloading / failed / blocked): size hint and shown size fit."""
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                width, height = (int(value) for value in size.split("x"))
                for name, dialog in result["dialogs"].items():
                    with self.subTest(dpr=dpr, size=size, dialog=name):
                        self.assertLessEqual(dialog["hint"][0], width)
                        self.assertLessEqual(dialog["hint"][1], height)
                        self.assertLessEqual(dialog["size"][0], width)
                        self.assertLessEqual(dialog["size"][1], height)
                        self.assertGreaterEqual(dialog["size"][0], dialog["hint"][0])
                        self.assertGreaterEqual(dialog["size"][1], dialog["hint"][1])

    def test_dialogs_not_clipped(self):
        for dpr in DPRS:
            for size, result in self.results[dpr].items():
                for name, dialog in result["dialogs"].items():
                    with self.subTest(dpr=dpr, size=size, dialog=name):
                        self.assertEqual(dialog["problems"], [])

    def test_large_screens_show_full_notice(self):
        for dpr in DPRS:
            self.assertFalse(self.results[dpr]["1920x1080"]["compact_notice"])


if __name__ == "__main__":
    unittest.main()

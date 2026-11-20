import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.converter import (
    ConversionCallbacks,
    Conversion,
    ConverterConfig,
    Phase,
    build_command,
    parse_line,
    sanitize_filename,
)
from core.errors import ErrorKind

FAKE = Path(__file__).with_name("fake_ytdlp.py")
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def wait_until(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.lock = threading.Lock()

    def callbacks(self) -> ConversionCallbacks:
        return ConversionCallbacks(
            on_title=lambda value: self.add("title", value),
            on_progress=lambda value: self.add("progress", value),
            on_phase=lambda value: self.add("phase", value),
        )

    def add(self, kind: str, value: object) -> None:
        with self.lock:
            self.events.append((kind, value))

    def values(self, kind: str) -> list:
        with self.lock:
            return [value for event_kind, value in self.events if event_kind == kind]


class TempDirsMixin:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.out_dir = root / "out"
        self.temp_root = root / "tmp"
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.ffmpeg = self.bin_dir / "ffmpeg.exe"
        self.ffmpeg.write_bytes(b"")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def config(self, scenario: str = "success", title: str = "My Song", **overrides) -> ConverterConfig:
        values = dict(
            output_dir=self.out_dir,
            temp_root=self.temp_root,
            ytdlp=None,
            ffmpeg=self.ffmpeg,
            command_prefix=(sys.executable, str(FAKE), scenario, title),
        )
        values.update(overrides)
        return ConverterConfig(**values)


class BuildCommandTests(TempDirsMixin, unittest.TestCase):
    def test_core_options(self):
        config = self.config(ytdlp=Path("C:/tools/yt-dlp.exe"), command_prefix=None)
        command = build_command(config, URL, Path("C:/work/job1"))
        self.assertEqual(command[0], str(Path("C:/tools/yt-dlp.exe")))
        for option in ("--no-playlist", "--newline", "--extract-audio", "--ignore-config"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("--audio-format") + 1], "mp3")
        self.assertEqual(command[command.index("--audio-quality") + 1], "5")
        self.assertEqual(command[command.index("--paths") + 1], str(Path("C:/work/job1")))
        self.assertEqual(command[-2:], ["--", URL])

    def test_ffmpeg_location_is_its_folder(self):
        command = build_command(self.config(), URL, self.temp_root)
        self.assertEqual(command[command.index("--ffmpeg-location") + 1], str(self.bin_dir))

    def test_no_ffmpeg_option_when_missing(self):
        command = build_command(self.config(ffmpeg=None), URL, self.temp_root)
        self.assertNotIn("--ffmpeg-location", command)

    def test_cookies_present(self):
        cookies = self.bin_dir / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        command = build_command(self.config(cookies=cookies), URL, self.temp_root)
        self.assertEqual(command[command.index("--cookies") + 1], str(cookies))

    def test_cookies_absent(self):
        command = build_command(self.config(cookies=None), URL, self.temp_root)
        self.assertNotIn("--cookies", command)

    def test_cookies_configured_but_file_gone(self):
        missing = self.bin_dir / "cookies.txt"
        command = build_command(self.config(cookies=missing), URL, self.temp_root)
        self.assertNotIn("--cookies", command)

    def test_node_runtime(self):
        node = self.bin_dir / "node.exe"
        command = build_command(self.config(node=node), URL, self.temp_root)
        self.assertEqual(command[command.index("--js-runtimes") + 1], f"node:{node}")
        command = build_command(self.config(node=None), URL, self.temp_root)
        self.assertNotIn("--js-runtimes", command)

    def test_command_prefix_replaces_executable(self):
        command = build_command(self.config(), URL, self.temp_root)
        self.assertEqual(command[:4], [sys.executable, str(FAKE), "success", "My Song"])

    def test_no_executable(self):
        with self.assertRaises(ValueError):
            build_command(self.config(command_prefix=None), URL, self.temp_root)


class ParseLineTests(unittest.TestCase):
    def test_title_and_file(self):
        self.assertEqual(parse_line("YTMP3:TITLE:Song | Live ").value, "Song | Live")
        event = parse_line("YTMP3:FILE:C:\\tmp\\abc.mp3\r\n")
        self.assertEqual((event.kind, event.value), ("file", "C:\\tmp\\abc.mp3"))

    def test_download_progress(self):
        event = parse_line("YTMP3:DL:downloading|250|1000|NA")
        self.assertEqual(event.kind, "progress")
        self.assertAlmostEqual(event.percent, 25.0)

    def test_progress_uses_estimate_when_total_unknown(self):
        event = parse_line("YTMP3:DL:downloading|50|NA|200.5")
        self.assertAlmostEqual(event.percent, 50 * 100 / 200.5)

    def test_progress_unknown_total(self):
        event = parse_line("YTMP3:DL:downloading|50|NA|NA")
        self.assertEqual(event.kind, "progress")
        self.assertIsNone(event.percent)

    def test_progress_is_clamped(self):
        self.assertEqual(parse_line("YTMP3:DL:downloading|2000|1000|NA").percent, 100.0)

    def test_download_finished_and_postprocess(self):
        self.assertEqual(parse_line("YTMP3:DL:finished|1000|1000|NA").kind, "download_finished")
        event = parse_line("YTMP3:PP:started|ExtractAudio")
        self.assertEqual((event.kind, event.value), ("postprocess", "started|ExtractAudio"))

    def test_marker_after_noise(self):
        self.assertEqual(parse_line("\x1b[K YTMP3:DL:downloading|1|4|NA").percent, 25.0)

    def test_other_lines(self):
        for line in ("[youtube] Extracting URL", "", "ERROR: Video unavailable",
                     "YTMP3:DL:error|NA|NA|NA", "YTMP3:DL:downloading|garbage"):
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))


class SanitizeTests(unittest.TestCase):
    def test_invalid_characters(self):
        self.assertEqual(sanitize_filename('AC/DC: Back <In> Black? "Live" | 1980*'),
                         "AC DC Back In Black Live 1980")

    def test_unicode_kept(self):
        self.assertEqual(sanitize_filename("周杰伦 - 晴天 (Official)"), "周杰伦 - 晴天 (Official)")

    def test_trailing_dots_and_spaces(self):
        self.assertEqual(sanitize_filename("  Song...  "), "Song")

    def test_empty_uses_fallback(self):
        self.assertEqual(sanitize_filename("???", fallback="dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(sanitize_filename(""), "audio")

    def test_reserved_names(self):
        self.assertEqual(sanitize_filename("CON"), "CON_")
        self.assertEqual(sanitize_filename("nul.txt"), "nul.txt_")

    def test_length_limit(self):
        self.assertLessEqual(len(sanitize_filename("a" * 400)), 150)

    def test_control_characters(self):
        self.assertEqual(sanitize_filename("a\tb\nc"), "a b c")


class ConversionTests(TempDirsMixin, unittest.TestCase):
    def run_conversion(self, config: ConverterConfig, job_id: str = "job1"):
        recorder = Recorder()
        result = Conversion(config, URL, job_id, recorder.callbacks()).run()
        return result, recorder

    def test_success(self):
        result, recorder = self.run_conversion(self.config(title="AC/DC: Highway?"))
        self.assertEqual(result.phase, Phase.DONE)
        self.assertIsNone(result.error)
        self.assertEqual(result.output_path, self.out_dir / "AC DC Highway.mp3")
        self.assertEqual(result.output_path.read_bytes(), b"ID3 fake mp3")
        self.assertEqual(recorder.values("title"), ["AC/DC: Highway?"])
        self.assertEqual(
            recorder.values("phase"), [Phase.DOWNLOADING, Phase.CONVERTING, Phase.DONE]
        )
        percents = [value for value in recorder.values("progress") if value is not None]
        self.assertEqual(percents[:3], [0.0, 25.0, 50.0])
        self.assertEqual(percents[-1], 100.0)
        self.assertFalse((self.temp_root / "job1").exists())

    def test_unicode_title_survives_the_pipe(self):
        result, _ = self.run_conversion(self.config(title="周杰伦 - 晴天"))
        self.assertEqual(result.output_path.name, "周杰伦 - 晴天.mp3")

    def test_name_clash_appends_number(self):
        self.out_dir.mkdir(parents=True)
        (self.out_dir / "My Song.mp3").write_bytes(b"old")
        (self.out_dir / "My Song (2).mp3").write_bytes(b"old")
        first, _ = self.run_conversion(self.config(), "job1")
        second, _ = self.run_conversion(self.config(), "job2")
        self.assertEqual(first.output_path.name, "My Song (3).mp3")
        self.assertEqual(second.output_path.name, "My Song (4).mp3")
        self.assertEqual((self.out_dir / "My Song.mp3").read_bytes(), b"old")

    def test_failure_maps_to_friendly_error(self):
        message = "[youtube] dQw4w9WgXcQ: Video unavailable. This video is private"
        result, recorder = self.run_conversion(self.config("fail", message))
        self.assertEqual(result.phase, Phase.FAILED)
        self.assertEqual(result.error.kind, ErrorKind.UNAVAILABLE)
        self.assertIn("Video unavailable", result.error.details)
        self.assertIsNone(result.output_path)
        self.assertEqual(recorder.values("phase")[-1], Phase.FAILED)
        self.assertFalse((self.temp_root / "job1").exists())
        self.assertFalse(self.out_dir.exists() and any(self.out_dir.iterdir()))

    def test_failure_sign_in(self):
        result, _ = self.run_conversion(
            self.config("fail", "[youtube] x: Sign in to confirm you're not a bot")
        )
        self.assertEqual(result.error.kind, ErrorKind.SIGN_IN)

    def test_exit_zero_without_mp3_fails(self):
        result, _ = self.run_conversion(self.config("no_mp3"))
        self.assertEqual(result.phase, Phase.FAILED)
        self.assertEqual(result.error.kind, ErrorKind.UNKNOWN)

    def test_missing_ytdlp(self):
        result, _ = self.run_conversion(self.config(command_prefix=None, ytdlp=None))
        self.assertEqual(result.phase, Phase.FAILED)
        self.assertEqual(result.error.kind, ErrorKind.CONVERTER_MISSING)
        self.assertIn("yt-dlp", result.error.details)

    def test_missing_ffmpeg(self):
        result, _ = self.run_conversion(self.config(ffmpeg=None))
        self.assertEqual(result.error.kind, ErrorKind.CONVERTER_MISSING)

    def test_stop_before_start(self):
        conversion = Conversion(self.config(), URL, "job1")
        conversion.stop()
        self.assertEqual(conversion.run().phase, Phase.STOPPED)
        self.assertFalse(self.out_dir.exists())

    def stop_while(self, scenario: str, wanted_phase: Phase) -> None:
        pid_file = Path(self.temp.name) / "pids.json"
        recorder = Recorder()
        conversion = Conversion(self.config(scenario), URL, "job1", recorder.callbacks())
        results = []
        with patch.dict(os.environ, {"FAKE_YTDLP_PIDS": str(pid_file)}):
            worker = threading.Thread(target=lambda: results.append(conversion.run()))
            worker.start()
            self.assertTrue(wait_until(
                lambda: pid_file.exists() and wanted_phase in recorder.values("phase")
            ))
        pids = json.loads(pid_file.read_text())
        self.assertTrue((self.temp_root / "job1").exists())

        started = time.monotonic()
        conversion.stop()
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 10)

        result = results[0]
        self.assertEqual(result.phase, Phase.STOPPED)
        self.assertIsNone(result.error)
        self.assertEqual(recorder.values("phase")[-1], Phase.STOPPED)
        self.assertFalse((self.temp_root / "job1").exists(), "partial files left behind")
        self.assertFalse(self.out_dir.exists() and any(self.out_dir.iterdir()))
        for pid in pids:
            self.assertTrue(wait_until(lambda: not process_alive(pid), 5), f"pid {pid} alive")

    def test_stop_during_download_kills_process_tree(self):
        self.stop_while("sleep_download", Phase.DOWNLOADING)

    def test_stop_during_conversion(self):
        self.stop_while("sleep_convert", Phase.CONVERTING)

    def test_arguments_reach_the_process(self):
        args_file = Path(self.temp.name) / "args.json"
        with patch.dict(os.environ, {"FAKE_YTDLP_ARGS": str(args_file)}):
            self.run_conversion(self.config())
        args = json.loads(args_file.read_text(encoding="utf-8"))
        self.assertIn("--no-playlist", args)
        self.assertEqual(args[-1], URL)


if __name__ == "__main__":
    unittest.main()

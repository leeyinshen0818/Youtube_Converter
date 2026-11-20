"""One-file exe behaviour: tool lookup in sys._MEIPASS\\bin, nothing written
into that temporary folder, a clean environment for yt-dlp, and the
single-instance guard. No network, no real yt-dlp, no real app-data folder."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core import tools, updater
from core.converter import _subprocess_env, child_env
from core.settings import settings_path
from core.single_instance import SingleInstance
from tests import updater_support as fake

ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = ("ffmpeg", "ffprobe", "node", "yt-dlp")


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class OnefileLayoutTests(unittest.TestCase):
    """dist\\YouTubeMP3Converter.exe copied alone to a shop PC; each launch
    unpacks into %TEMP%\\_MEIxxxxxx (sys._MEIPASS) with the tools in bin\\."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.shop = root / "Desktop"
        self.exe = touch(self.shop / "YouTubeMP3Converter.exe")
        self.meipass = root / "Temp" / "_MEI123456"
        for name in TOOL_NAMES:
            touch(self.meipass / "bin" / f"{name}.exe")
        self.local = root / "LocalAppData"
        self.home = root / "Home"
        self.local.mkdir()
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def frozen_env(self) -> tools.ToolEnvironment:
        with patch.object(tools.sys, "frozen", True, create=True), \
                patch.object(tools.sys, "executable", str(self.exe)), \
                patch.object(tools.sys, "_MEIPASS", str(self.meipass), create=True), \
                patch.dict("os.environ", {"LOCALAPPDATA": str(self.local)}):
            env = tools.default_environment()
        return tools.ToolEnvironment(env.bundle_dir, env.app_dir, env.local_app_data, self.home, lambda _n: None)

    def test_environment_points_at_exe_folder_and_meipass(self):
        env = self.frozen_env()
        self.assertEqual(env.app_dir, self.shop.resolve())
        self.assertEqual(env.bundle_dir, self.meipass)
        self.assertEqual(env.local_app_data, self.local)

    def test_all_four_tools_found_in_meipass_bin(self):
        found = tools.locate_tools(self.frozen_env())
        self.assertEqual(found.missing, [])
        for path in (found.ffmpeg, found.ffprobe, found.node, found.ytdlp):
            self.assertEqual(path.parent, self.meipass / "bin")

    def test_updated_ytdlp_in_app_data_wins(self):
        env = self.frozen_env()
        updated = touch(tools.user_tools_dir(env) / "yt-dlp.exe")
        self.assertEqual(updated, self.local / "YouTube MP3 Converter" / "tools" / "yt-dlp.exe")
        self.assertEqual(tools.find_ytdlp(env), updated)

    def test_nothing_is_written_below_the_temporary_folder(self):
        from ui.services import AppServices

        env = self.frozen_env()
        writable = [
            tools.app_data_dir(env),
            tools.user_tools_dir(env),
            tools.temp_root(env),
            tools.ready_root(env),
            settings_path(env),
            AppServices.for_app(env).tools_dir,
        ]
        for path in writable:
            with self.subTest(path=path):
                self.assertFalse(path.is_relative_to(self.meipass))
                self.assertTrue(path.is_relative_to(self.local))

    def test_update_into_app_data_leaves_meipass_untouched(self):
        env = self.frozen_env()
        before = sorted((p.relative_to(self.meipass), p.stat().st_size) for p in self.meipass.rglob("*"))
        exe = fake.script(fake.TAG)
        result = updater.update_ytdlp(
            updater.UpdateRequest(
                tools_dir=tools.user_tools_dir(env),
                current_exe=tools.find_ytdlp(env),  # the bundled copy in _MEIPASS\bin
                opener=fake.release_opener(exe),
                command_for=fake.run_with_python,
            )
        )
        self.assertEqual(result.kind, updater.UpdateKind.UPDATED)
        after = sorted((p.relative_to(self.meipass), p.stat().st_size) for p in self.meipass.rglob("*"))
        self.assertEqual(after, before)
        self.assertEqual(tools.find_ytdlp(env), tools.user_tools_dir(env) / "yt-dlp.exe")


class ChildEnvironmentTests(unittest.TestCase):
    BOOTLOADER = {
        "_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI1",
        "_PYI_ARCHIVE_FILE": r"C:\shop\YouTubeMP3Converter.exe",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "_MEIPASS2": r"C:\Temp\_MEI1",
        "PATH": r"C:\Windows",
    }

    def test_bootloader_variables_are_dropped(self):
        env = child_env(self.BOOTLOADER)
        self.assertEqual(env["PATH"], r"C:\Windows")
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertFalse([key for key in env if key.startswith("_PYI_") or key == "_MEIPASS2"])

    def test_converter_env_is_clean(self):
        with patch.dict("os.environ", self.BOOTLOADER):
            env = _subprocess_env()
        self.assertNotIn("_PYI_ARCHIVE_FILE", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_version_check_runs_with_clean_env(self):
        script = textwrap.dedent(
            """
            import os, sys
            bad = [k for k in os.environ if k.startswith("_PYI_")]
            if bad or os.environ.get("PYINSTALLER_RESET_ENVIRONMENT") != "1":
                sys.exit(3)
            print("2025.10.22")
            """
        )
        with tempfile.TemporaryDirectory() as temp:
            exe = Path(temp) / "yt-dlp.exe"
            exe.write_text(script, encoding="utf-8")
            with patch.dict("os.environ", {"_PYI_ARCHIVE_FILE": "x", "_PYI_PARENT_PROCESS_LEVEL": "1"}):
                version = updater.read_version(exe, fake.run_with_python)
        self.assertEqual(version, "2025.10.22")


HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from core.single_instance import SingleInstance
    guard = SingleInstance(sys.argv[2])
    print("acquired" if guard.acquire() else "busy", flush=True)
    if sys.argv[3] == "hold":
        time.sleep(60)
    """
)


class SingleInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        # A unique name: never collides with a real running app.
        self.name = f"Local\\YouTubeMP3Converter.Test.{uuid.uuid4().hex}"

    def run_child(self, mode: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(ROOT), self.name, mode],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

    def test_second_holder_in_same_process_is_refused_until_release(self):
        first, second = SingleInstance(self.name), SingleInstance(self.name)
        self.assertTrue(first.acquire())
        self.assertTrue(first.held)
        self.assertFalse(second.acquire())
        self.assertFalse(second.held)
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_acquire_twice_on_same_guard_is_fine(self):
        guard = SingleInstance(self.name)
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.acquire())
        guard.release()
        guard.release()  # harmless

    def test_other_process_is_refused_while_held(self):
        guard = SingleInstance(self.name)
        self.assertTrue(guard.acquire())
        try:
            child = self.run_child("once")
            out, _ = child.communicate(timeout=60)
            self.assertEqual(out.strip(), "busy")
        finally:
            guard.release()

    def test_released_when_holder_exits_normally(self):
        child = self.run_child("once")
        out, _ = child.communicate(timeout=60)
        self.assertEqual(out.strip(), "acquired")
        guard = SingleInstance(self.name)
        self.assertTrue(guard.acquire())
        guard.release()

    def test_released_when_holder_crashes(self):
        child = self.run_child("hold")
        try:
            self.assertEqual(child.stdout.readline().strip(), "acquired")
            self.assertFalse(SingleInstance(self.name).acquire())
        finally:
            child.kill()  # TerminateProcess: no clean-up code runs
            child.communicate(timeout=60)
        guard = SingleInstance(self.name)
        self.assertTrue(guard.acquire())
        guard.release()


class MainGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = f"Local\\YouTubeMP3Converter.Test.{uuid.uuid4().hex}"

    def test_second_launch_exits_quietly_without_a_window(self):
        from ui import app

        holder = SingleInstance(self.name)
        self.assertTrue(holder.acquire())
        try:
            with patch.object(app, "_run", side_effect=AssertionError("a second window was started")):
                self.assertEqual(app.main([], instance_name=self.name), 0)
        finally:
            holder.release()

    def test_guard_released_after_normal_exit_and_after_an_error(self):
        from ui import app

        with patch.object(app, "_run", return_value=0):
            self.assertEqual(app.main([], instance_name=self.name), 0)
        probe = SingleInstance(self.name)
        self.assertTrue(probe.acquire())
        probe.release()
        with patch.object(app, "_run", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                app.main([], instance_name=self.name)
        guard = SingleInstance(self.name)
        self.assertTrue(guard.acquire())
        guard.release()

    def test_real_app_uses_the_shared_name(self):
        from core.single_instance import APP_MUTEX_NAME
        from ui import app

        self.assertTrue(APP_MUTEX_NAME.startswith("Local\\"))
        self.assertEqual(app.main.__defaults__[-1], APP_MUTEX_NAME)


if __name__ == "__main__":
    unittest.main()

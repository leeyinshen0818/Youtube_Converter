import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tools
from core.tools import ToolEnvironment


class ToolLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bundle = root / "bundle"
        self.app = root / "app"
        self.local = root / "local"
        self.home = root / "home"
        self.path_dir = root / "path"
        for folder in (self.bundle, self.app, self.local, self.home, self.path_dir):
            folder.mkdir()
        self.env = ToolEnvironment(
            bundle_dir=self.bundle,
            app_dir=self.app,
            local_app_data=self.local,
            home=self.home,
            which=self.fake_which,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_which(self, name: str) -> str | None:
        candidate = self.path_dir / f"{name}.exe"
        return str(candidate) if candidate.exists() else None

    def touch(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    @property
    def winget(self) -> Path:
        return self.home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"

    # ffmpeg / ffprobe: bundled -> PATH -> WinGet links

    def test_nothing_found(self):
        found = tools.locate_tools(self.env)
        self.assertEqual(found.missing, ["ffmpeg", "ffprobe", "node", "yt-dlp"])

    def test_media_tool_order(self):
        for name, finder in (("ffmpeg", tools.find_ffmpeg), ("ffprobe", tools.find_ffprobe)):
            with self.subTest(name=name):
                winget = self.touch(self.winget / f"{name}.exe")
                self.assertEqual(finder(self.env), winget)
                on_path = self.touch(self.path_dir / f"{name}.exe")
                self.assertEqual(finder(self.env), on_path)
                bundled = self.touch(self.bundle / "bin" / f"{name}.exe")
                self.assertEqual(finder(self.env), bundled)

    def test_winget_oserror_is_not_found(self):
        # The old app returned the WinGet folder when the check raised OSError.
        self.touch(self.winget / "ffmpeg.exe")
        original = Path.is_file

        def failing_is_file(path: Path) -> bool:
            if "WinGet" in str(path):
                raise OSError("The file cannot be accessed by the system")
            return original(path)

        with patch.object(Path, "is_file", failing_is_file):
            self.assertIsNone(tools.find_ffmpeg(self.env))

    def test_winget_folder_without_ffmpeg_is_not_found(self):
        self.winget.mkdir(parents=True)
        self.assertIsNone(tools.find_ffmpeg(self.env))

    def test_path_entry_that_is_not_a_file_is_ignored(self):
        (self.path_dir / "ffmpeg.exe").mkdir()
        self.assertIsNone(tools.find_ffmpeg(self.env))

    # node: bundled -> PATH

    def test_node_order(self):
        on_path = self.touch(self.path_dir / "node.exe")
        self.assertEqual(tools.find_node(self.env), on_path)
        bundled = self.touch(self.bundle / "bin" / "node.exe")
        self.assertEqual(tools.find_node(self.env), bundled)

    def test_node_ignores_winget(self):
        self.touch(self.winget / "node.exe")
        self.assertIsNone(tools.find_node(self.env))

    # yt-dlp: updated copy -> copy shipped with the app -> PATH

    def test_ytdlp_order(self):
        on_path = self.touch(self.path_dir / "yt-dlp.exe")
        self.assertEqual(tools.find_ytdlp(self.env), on_path)

        bundled_bin = self.touch(self.bundle / "bin" / "yt-dlp.exe")
        self.assertEqual(tools.find_ytdlp(self.env), bundled_bin)

        app_bin = self.touch(self.app / "bin" / "yt-dlp.exe")
        self.assertEqual(tools.find_ytdlp(self.env), app_bin)

        next_to_app = self.touch(self.app / "yt-dlp.exe")
        self.assertEqual(tools.find_ytdlp(self.env), next_to_app)

        updated = self.touch(self.local / "YouTube MP3 Converter" / "tools" / "yt-dlp.exe")
        self.assertEqual(tools.find_ytdlp(self.env), updated)

    def test_user_tools_dir(self):
        self.assertEqual(
            tools.user_tools_dir(self.env), self.local / "YouTube MP3 Converter" / "tools"
        )

    def test_app_data_subfolders(self):
        base = self.local / "YouTube MP3 Converter"
        self.assertEqual(tools.app_data_dir(self.env), base)
        self.assertEqual(tools.temp_root(self.env), base / "tmp")

    def test_cookies_lookup_order(self):
        self.assertIsNone(tools.find_cookies(self.env))
        next_to_app = self.touch(self.app / "cookies.txt")
        self.assertEqual(tools.find_cookies(self.env), next_to_app)
        imported = self.touch(self.local / "YouTube MP3 Converter" / "cookies.txt")
        self.assertEqual(tools.find_cookies(self.env), imported)

    def test_locate_tools_all_found(self):
        for name in ("ffmpeg", "ffprobe", "node", "yt-dlp"):
            self.touch(self.bundle / "bin" / f"{name}.exe")
        found = tools.locate_tools(self.env)
        self.assertEqual(found.missing, [])
        self.assertEqual(found.ytdlp, self.bundle / "bin" / "yt-dlp.exe")

    def test_default_environment_uses_localappdata(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": self.local.as_posix()}):
            env = tools.default_environment()
        self.assertEqual(env.local_app_data, Path(self.local.as_posix()))
        self.assertTrue((env.app_dir / "core" / "tools.py").is_file())


if __name__ == "__main__":
    unittest.main()

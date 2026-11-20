"""core.settings: defaults, corrupt files, save/load, writable check."""

import json
import tempfile
import unittest
from pathlib import Path

from core import tools
from core.saver import default_save_dir
from core.settings import (
    Settings,
    SettingsStore,
    check_writable,
    load_settings,
    save_settings,
    settings_path,
)
from core.tools import ToolEnvironment


class SettingsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "appdata" / "settings.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults(self):
        settings = Settings()
        self.assertIsNone(settings.custom_save_dir)
        self.assertTrue(settings.uses_default_save_dir)
        self.assertEqual(settings.save_dir, default_save_dir())
        self.assertEqual(default_save_dir().name, "YouTube MP3 Converter")
        self.assertEqual(default_save_dir().parent.name, "Downloads")

    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_settings(self.path), Settings())

    def test_corrupt_files_give_defaults(self):
        self.path.parent.mkdir(parents=True)
        for content in (b"{not json", b"\xff\xfe\x00garbage", b"[1, 2]", b"null", b"", b'"text"'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertEqual(load_settings(self.path), Settings())

    def test_invalid_values_give_defaults(self):
        self.path.parent.mkdir(parents=True)
        for value in (42, "", "   ", "relative/folder", None, ["C:/x"], {"a": 1}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps({"save_dir": value}), encoding="utf-8")
                self.assertEqual(load_settings(self.path), Settings())

    def test_folder_instead_of_file_gives_defaults(self):
        self.path.mkdir(parents=True)
        self.assertEqual(load_settings(self.path), Settings())

    def test_save_and_load(self):
        folder = self.root / "Music"
        save_settings(self.path, Settings(custom_save_dir=folder))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"save_dir": str(folder)})
        self.assertEqual(load_settings(self.path).save_dir, folder)
        save_settings(self.path, Settings())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"save_dir": None})
        self.assertEqual(load_settings(self.path), Settings())
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["settings.json"])  # no temp left

    def test_unknown_keys_and_bom_are_tolerated(self):
        self.path.parent.mkdir(parents=True)
        folder = self.root / "Music"
        text = json.dumps({"save_dir": str(folder), "future": True})
        self.path.write_bytes(b"\xef\xbb\xbf" + text.encode())
        self.assertEqual(load_settings(self.path).save_dir, folder)

    def test_settings_path_is_in_app_data(self):
        env = ToolEnvironment(bundle_dir=self.root, app_dir=self.root, local_app_data=self.root / "local", home=self.root)
        self.assertEqual(settings_path(env), tools.app_data_dir(env) / "settings.json")


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "appdata" / "settings.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_store_persists_and_resets(self):
        store = SettingsStore(self.path)
        self.assertEqual(store.save_dir, default_save_dir())
        folder = self.root / "Music"
        store.set_save_dir(folder)
        self.assertEqual(store.save_dir, folder)
        self.assertEqual(SettingsStore(self.path).save_dir, folder)
        store.reset_save_dir()
        self.assertEqual(store.save_dir, default_save_dir())
        self.assertTrue(SettingsStore(self.path).settings.uses_default_save_dir)

    def test_failed_write_keeps_new_value_in_memory(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file where the folder should be")
        store = SettingsStore(blocker / "settings.json")
        with self.assertRaises(OSError):
            store.set_save_dir(self.root / "Music")
        self.assertEqual(store.save_dir, self.root / "Music")

    def test_in_memory_store_writes_nothing(self):
        store = SettingsStore.in_memory(self.root / "Start")
        self.assertEqual(store.save_dir, self.root / "Start")
        store.set_save_dir(self.root / "Other")
        self.assertEqual(store.save_dir, self.root / "Other")
        self.assertEqual(list(self.root.iterdir()), [])


class WritableTests(unittest.TestCase):
    def test_writable_folder_is_created_and_left_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "new" / "folder"
            self.assertTrue(check_writable(folder))
            self.assertEqual(list(folder.iterdir()), [])

    def test_file_path_is_not_writable(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "file.txt"
            file.write_text("x")
            self.assertFalse(check_writable(file))
            self.assertFalse(check_writable(file / "below"))


if __name__ == "__main__":
    unittest.main()

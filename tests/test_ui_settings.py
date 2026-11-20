"""Stage 4: Settings and Update dialogs, save folder used by the window
(offscreen, temp folders, fake opener, fake yt-dlp; no network)."""

import tempfile
import threading
import unittest
from pathlib import Path

from tests.qt_support import process_events, qt_app

from core import errors  # noqa: E402
from core.cookies import CookieStatus  # noqa: E402
from core.jobqueue import ItemStatus, JobQueue  # noqa: E402
from core.saver import default_save_dir  # noqa: E402
from core.settings import SettingsStore, load_settings  # noqa: E402
from core.updater import CHUNK_BYTES, Stage, UpdateKind, UpdateResult  # noqa: E402
from tests.test_ui_actions import LINKS, FakeShell, StagedFake, wait_for  # noqa: E402
from tests.updater_support import (  # noqa: E402
    TAG,
    FakeOpener,
    release_opener,
    run_with_python,
    script,
    write_exe,
)
from ui.dialog_texts import (  # noqa: E402
    BLOCKED_TEXT,
    COOKIES_HELP,
    NOT_WRITABLE_TEXT,
    UPDATE_LINK_TEXT,
    app_version_text,
    component_version_text,
    cookie_status_text,
    current_version_text,
    progress_state,
    result_text,
    stage_text,
)
from ui.main_window import MainWindow  # noqa: E402
from ui.services import NOT_INSTALLED, AppServices, VersionCache  # noqa: E402

VALID_COOKIES = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
OLD = "2024.01.01"


# --- pure helpers ---------------------------------------------------------------


class DialogTextTests(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(cookie_status_text(CookieStatus.IMPORTED), "Cookies: imported")
        self.assertEqual(cookie_status_text(CookieStatus.NOT_IMPORTED), "Cookies: not imported")
        self.assertEqual(cookie_status_text(CookieStatus.NEXT_TO_APP), "Cookies: found next to the app")
        self.assertEqual(app_version_text("2.0.0-dev"), "App 2.0.0-dev")
        self.assertEqual(component_version_text("2025.10.22"), "Download component (yt-dlp): 2025.10.22")
        self.assertEqual(component_version_text(NOT_INSTALLED), "Download component (yt-dlp): not installed")
        self.assertEqual(current_version_text("2025.10.22"), "Current version: 2025.10.22")
        self.assertEqual(
            COOKIES_HELP,
            "Only needed if YouTube asks you to sign in. This file contains login information; keep it private.",
        )

    def test_result_texts(self):
        updated = result_text(UpdateResult(UpdateKind.UPDATED, version=TAG))
        self.assertEqual(updated.text, f"Updated to {TAG}. You can try again now.")
        latest = result_text(UpdateResult(UpdateKind.ALREADY_LATEST, version=TAG))
        self.assertEqual(latest.text, f"You already have the latest version ({TAG}).")
        failed = result_text(UpdateResult(UpdateKind.FAILED, message="Couldn't update. Your current version still works.",
                                          details="HTTP error 403: rate limit"))
        self.assertEqual(failed.text, "Couldn't update. Your current version still works.")
        self.assertEqual(failed.tooltip, "HTTP error 403: rate limit")
        self.assertNotIn("HTTP", failed.text)
        self.assertEqual(result_text(UpdateResult(UpdateKind.CANCELLED)).role, "message")

    def test_progress_state(self):
        self.assertEqual(progress_state(Stage.CHECKING, 0, None).maximum, 0)  # busy
        self.assertEqual(progress_state(Stage.DOWNLOADING, 5, None).maximum, 0)
        state = progress_state(Stage.DOWNLOADING, 50, 200)
        self.assertEqual((state.maximum, state.value), (100, 25))
        self.assertEqual(progress_state(Stage.DOWNLOADING, 300, 200).value, 100)
        self.assertEqual(progress_state(Stage.VERIFYING, 1, 1).maximum, 0)
        for stage in Stage:
            self.assertNotIn("yt-dlp", stage_text(stage))

    def test_failed_row_hints_match_the_link(self):
        self.assertEqual(UPDATE_LINK_TEXT, "Downloads not working? Update")
        self.assertIn(f'"{UPDATE_LINK_TEXT}"', errors.classify_error("weird failure").hint)
        self.assertIn(f'"{UPDATE_LINK_TEXT}"', errors.missing_tool_error("yt-dlp").hint)


# --- window with temp services ------------------------------------------------------


class Base(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ready = self.root / "ready"
        self.app_data = self.root / "appdata"
        self.tools = self.app_data / "tools"
        self.ytdlp: Path | None = None
        self.opener = FakeOpener()
        self.services = AppServices(
            settings=SettingsStore(self.app_data / "settings.json"),
            app_data=self.app_data,
            app_dir=self.root / "app",
            tools_dir=self.tools,
            find_ytdlp=lambda: self.ytdlp,
            make_opener=lambda: self.opener,
            command_for=run_with_python,
            version_timeout=30,
        )
        StagedFake.ready_root = self.ready
        StagedFake.outcome = "done"
        self.shell = FakeShell()
        self.window = MainWindow(
            queue_factory=lambda bridge: JobQueue(StagedFake, staging_root=self.ready, **bridge.callbacks()),
            temp_root=self.root / "tmp",
            ready_root=self.ready,
            shell=self.shell,
            services=self.services,
        )
        self.window.confirm = lambda *args: True
        self.window.resize(1200, 800)
        self.window.show()

    def tearDown(self) -> None:
        for dialog in (self.window.settings_dialog, self.window.update_dialog):
            try:
                if dialog is None:
                    continue
                runner = getattr(dialog, "runner", None)
                if runner is not None:
                    runner.cancel.set()
                    if getattr(self.opener, "gate", None) is not None:
                        self.opener.gate.set()
                    runner.thread.join(10)
                process_events()
                dialog.close()
            except RuntimeError:  # already deleted
                pass
        self.window.versions.join(10)
        self.window.queue.shutdown()
        self.window.queue.join(5)
        self.window.close()
        wait_for(lambda: not self.window.isVisible(), 8)
        process_events()
        self.temp.cleanup()

    def convert(self, count: int) -> None:
        self.window.input_panel.input.setPlainText("\n".join(LINKS[:count]))
        self.window.input_panel.convert()
        self.assertTrue(wait_for(lambda: sum(1 for i in self.window.model.items() if i.status is ItemStatus.READY) == count))


class WindowEntryPointTests(Base):
    def test_top_bar_and_footer(self):
        self.assertTrue(self.window.settings_button.isVisible())
        self.assertEqual(self.window.settings_button.text(), "Settings")
        self.assertTrue(self.window.update_link.isVisible())
        self.assertEqual(self.window.update_link.text(), "Downloads not working? Update")
        self.window.settings_button.click()
        self.assertIsNotNone(self.window.settings_dialog)
        self.assertTrue(self.window.settings_dialog.isModal())
        self.window.settings_dialog.close()
        process_events()
        self.window.update_link.click()
        self.assertIsNotNone(self.window.update_dialog)
        self.assertTrue(self.window.update_dialog.isModal())

    def test_bare_window_never_touches_real_folders(self):
        window = MainWindow(queue_factory=lambda bridge: JobQueue(StagedFake, **bridge.callbacks()))
        try:
            self.assertIsNone(window.services.settings.path)
            self.assertEqual(window.save_dir, default_save_dir())
            self.assertIsNone(window.services.find_ytdlp())
            self.assertNotIn("YouTube MP3 Converter", str(window.services.app_data))
        finally:
            window.close()

    def test_save_dir_and_services_are_exclusive(self):
        with self.assertRaises(ValueError):
            MainWindow(save_dir=self.root, services=self.services)


class SettingsDialogTests(Base):
    def open(self):
        dialog = self.window.open_settings()
        process_events()
        return dialog

    def test_change_and_reset_save_folder(self):
        dialog = self.open()
        self.assertEqual(dialog.folder_label.text(), str(default_save_dir()))
        self.assertEqual(dialog.folder_label.toolTip(), str(default_save_dir()))
        self.assertFalse(dialog.reset_button.isEnabled())
        music = self.root / "Music"
        dialog.choose_folder = lambda: music
        dialog.change_button.click()
        self.assertEqual(self.window.save_dir, music)
        self.assertEqual(load_settings(self.app_data / "settings.json").save_dir, music)
        self.assertEqual(dialog.folder_label.text(), str(music))
        self.assertTrue(dialog.reset_button.isEnabled())
        self.assertTrue(dialog.folder_message.isHidden())
        self.assertEqual(self.window.queue_panel.delegate.save_location, "Music")  # "Saved to Music" rows

        dialog.reset_button.click()
        self.assertEqual(self.window.save_dir, default_save_dir())
        self.assertTrue(load_settings(self.app_data / "settings.json").uses_default_save_dir)

    def test_cancelled_folder_choice_changes_nothing(self):
        dialog = self.open()
        dialog.choose_folder = lambda: None
        self.assertFalse(dialog.change_folder())
        self.assertEqual(self.window.save_dir, default_save_dir())
        self.assertFalse((self.app_data / "settings.json").exists())

    def test_unwritable_folder_rejected(self):
        dialog = self.open()
        blocker = self.root / "a file"
        blocker.write_text("x")
        dialog.choose_folder = lambda: blocker
        self.assertFalse(dialog.change_folder())
        self.assertEqual(dialog.folder_message.text(), NOT_WRITABLE_TEXT)
        self.assertFalse(dialog.folder_message.isHidden())
        self.assertEqual(self.window.save_dir, default_save_dir())

    def test_cookies_import_and_remove(self):
        dialog = self.open()
        self.assertTrue(dialog.advanced.isHidden())  # collapsed by default
        dialog.advanced_button.click()
        self.assertFalse(dialog.advanced.isHidden())
        self.assertEqual(dialog.cookie_status.text(), "Cookies: not imported")
        self.assertFalse(dialog.remove_button.isEnabled())

        bad = self.root / "bad.txt"
        bad.write_text("not cookies")
        dialog.choose_cookies_file = lambda: bad
        self.assertFalse(dialog.import_cookies())
        self.assertIn("doesn't look like a cookies.txt", dialog.cookie_message.text())
        self.assertEqual(dialog.cookie_status.text(), "Cookies: not imported")

        good = self.root / "cookies.txt"
        good.write_text(VALID_COOKIES)
        dialog.choose_cookies_file = lambda: good
        self.assertTrue(dialog.import_cookies())
        self.assertEqual(dialog.cookie_status.text(), "Cookies: imported")
        self.assertEqual((self.app_data / "cookies.txt").read_text(), VALID_COOKIES)
        self.assertNotIn("SID", dialog.cookie_message.text())
        self.assertTrue(dialog.remove_button.isEnabled())

        dialog.remove_button.click()
        self.assertEqual(dialog.cookie_status.text(), "Cookies: not imported")
        self.assertFalse((self.app_data / "cookies.txt").exists())
        self.assertTrue(good.exists())

    def test_cookies_next_to_app(self):
        (self.root / "app").mkdir()
        (self.root / "app" / "cookies.txt").write_text(VALID_COOKIES)
        dialog = self.open()
        self.assertEqual(dialog.cookie_status.text(), "Cookies: found next to the app")
        self.assertFalse(dialog.remove_button.isEnabled())

    def test_versions_line(self):
        self.ytdlp = write_exe(self.root / "bin" / "yt-dlp.exe", OLD)
        dialog = self.open()
        self.assertEqual(dialog.app_version.text(), app_version_text())
        self.assertTrue(wait_for(lambda: dialog.component_version.text().endswith(OLD)))
        # Cached for the session: a second dialog does not run yt-dlp again.
        self.ytdlp.write_bytes(script("2099.01.01"))
        dialog.close()
        process_events()
        second = self.open()
        self.assertEqual(second.component_version.text(), component_version_text(OLD))

    def test_versions_not_installed(self):
        dialog = self.open()
        self.assertTrue(wait_for(lambda: dialog.component_version.text().endswith("not installed")))

    def test_only_close_button(self):
        from PySide6.QtWidgets import QDialogButtonBox

        dialog = self.open()
        box = dialog.findChild(QDialogButtonBox)
        self.assertEqual([b.text() for b in box.buttons()], ["Close"])
        dialog.close_button.click()
        self.assertTrue(wait_for(lambda: self.window.settings_dialog is None))  # deleted on close


class SaveFolderFlowTests(Base):
    def use_folder(self, folder: Path) -> None:
        dialog = self.window.open_settings()
        dialog.choose_folder = lambda: folder
        self.assertTrue(dialog.change_folder())
        dialog.close()
        process_events()

    def test_download_uses_setting(self):
        music = self.root / "Music"
        self.use_folder(music)
        self.convert(1)
        item = self.window.model.item(0)
        from ui.queue_delegate import RowAction

        self.window.handle_action(item.id, RowAction.DOWNLOAD.value)
        self.assertTrue(wait_for(lambda: self.window.model.item(0).status is ItemStatus.SAVED))
        self.assertEqual(self.window.model.item(0).saved_path.parent, music)

    def test_download_all_and_message_use_setting(self):
        music = self.root / "Music"
        self.use_folder(music)
        self.convert(2)
        self.assertEqual(self.window.download_all(), 2)
        self.assertTrue(wait_for(lambda: len(list(music.glob("*.mp3"))) == 2))
        self.assertTrue(wait_for(lambda: self.window.queue_panel.done_line.text() == "2 MP3s saved to Music"))

    def test_open_folder_uses_setting(self):
        music = self.root / "Music"
        self.use_folder(music)
        self.window.open_folder()
        self.assertEqual(self.shell.calls[-1], ("open", music))
        dialog = self.window.open_settings()
        dialog.reset_button.click()
        dialog.close()
        self.window.open_folder()
        self.assertEqual(self.shell.calls[-1], ("open", default_save_dir()))


class UpdateDialogTests(Base):
    def open(self):
        dialog = self.window.open_update()
        process_events()
        return dialog

    def finish(self, dialog) -> UpdateResult:
        self.assertTrue(wait_for(lambda: dialog.result is not None, 30))
        return dialog.result

    def test_success(self):
        self.ytdlp = write_exe(self.tools / "yt-dlp.exe", OLD)
        self.opener = release_opener(script(TAG))
        dialog = self.open()
        self.assertTrue(wait_for(lambda: dialog.current_label.text() == f"Current version: {OLD}"))
        self.assertTrue(dialog.start_update())
        self.assertFalse(dialog.progress.isHidden())
        self.assertFalse(dialog.cancel_button.isHidden())
        result = self.finish(dialog)
        self.assertEqual(result.kind, UpdateKind.UPDATED)
        self.assertEqual(dialog.message.text(), f"Updated to {TAG}. You can try again now.")
        self.assertEqual(dialog.current_label.text(), f"Current version: {TAG}")
        self.assertTrue(dialog.progress.isHidden())
        self.assertTrue(dialog.cancel_button.isHidden())
        self.assertEqual(self.window.versions.value, TAG)

    def test_already_latest(self):
        self.ytdlp = write_exe(self.tools / "yt-dlp.exe", TAG)
        self.opener = release_opener(script(TAG))
        dialog = self.open()
        dialog.update_button.click()
        self.finish(dialog)
        self.assertEqual(dialog.message.text(), f"You already have the latest version ({TAG}).")

    def test_failure_is_friendly_with_details_in_tooltip(self):
        self.ytdlp = write_exe(self.tools / "yt-dlp.exe", OLD)
        dialog = self.open()  # FakeOpener() without routes: HTTP 404
        dialog.update_button.click()
        self.assertEqual(self.finish(dialog).kind, UpdateKind.FAILED)
        self.assertEqual(dialog.message.text(), "Couldn't update. Your current version still works.")
        self.assertIn("404", dialog.message.toolTip())
        self.assertEqual((self.tools / "yt-dlp.exe").read_bytes(), script(OLD))
        self.assertTrue(dialog.update_button.isEnabled())  # can try again

    def test_blocked_while_downloads_run(self):
        StagedFake.outcome = "hang"
        self.window.input_panel.input.setPlainText(LINKS[0])
        self.window.input_panel.convert()
        self.assertTrue(wait_for(self.window.downloads_running))
        self.opener = release_opener(script(TAG))
        dialog = self.open()
        self.assertEqual(dialog.message.text(), BLOCKED_TEXT)
        self.assertFalse(dialog.update_button.isEnabled())
        self.assertFalse(dialog.start_update())
        self.assertIsNone(dialog.runner)
        self.assertEqual(self.opener.requested, [])

        self.window.queue.stop_all()
        # The dialog notices by itself once the downloads have finished.
        self.assertTrue(wait_for(lambda: dialog.update_button.isEnabled()))
        self.assertFalse(dialog.refresh_blocked())
        self.assertNotEqual(dialog.message.text(), BLOCKED_TEXT)

    def gated_dialog(self):
        self.ytdlp = write_exe(self.tools / "yt-dlp.exe", OLD)
        self.opener = release_opener(script(TAG, padding=CHUNK_BYTES * 4))
        self.opener.gate = threading.Event()  # hold the download after one chunk
        dialog = self.open()
        dialog.start_update()
        self.assertTrue(wait_for(lambda: dialog.progress.maximum() == 100 and dialog.progress.value() > 0, 30))
        return dialog

    def test_cancel_during_download(self):
        dialog = self.gated_dialog()
        self.assertEqual(dialog.message.text(), "Downloading the new version...")
        dialog.cancel_button.click()
        self.assertEqual(dialog.message.text(), "Cancelling...")
        self.opener.gate.set()
        self.assertEqual(self.finish(dialog).kind, UpdateKind.CANCELLED)
        self.assertEqual(dialog.message.text(), "Update cancelled. Nothing was changed.")
        self.assertEqual((self.tools / "yt-dlp.exe").read_bytes(), script(OLD))
        self.assertEqual(sorted(p.name for p in self.tools.iterdir()), ["yt-dlp.exe"])

    def test_closing_mid_update_cancels_safely(self):
        dialog = self.gated_dialog()
        dialog.close()
        process_events()
        self.assertTrue(dialog.isVisible())  # stays until the worker cleaned up
        self.opener.gate.set()
        self.assertTrue(wait_for(lambda: self.window.update_dialog is None, 30))
        self.assertEqual((self.tools / "yt-dlp.exe").read_bytes(), script(OLD))
        self.assertEqual(sorted(p.name for p in self.tools.iterdir()), ["yt-dlp.exe"])

    def test_escape_mid_update_cancels_safely(self):
        dialog = self.gated_dialog()
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.runner.cancel.is_set())
        self.opener.gate.set()
        self.assertTrue(wait_for(lambda: self.window.update_dialog is None, 30))

    def test_no_technical_text_on_screen(self):
        from PySide6.QtWidgets import QAbstractButton, QLabel

        self.ytdlp = write_exe(self.tools / "yt-dlp.exe", OLD)
        dialog = self.open()
        dialog.update_button.click()
        self.finish(dialog)
        widgets = [*dialog.findChildren(QLabel), *dialog.findChildren(QAbstractButton)]
        shown = [w.text() for w in widgets if w.isVisible()]
        self.assertIn("Couldn't update. Your current version still works.", shown)
        for text in shown:
            for word in ("HTTP", "yt-dlp", "exe", "SHA", "github", "Error", "--version"):
                self.assertNotIn(word, text)


class VersionCacheTests(unittest.TestCase):
    def test_lookup_runs_once_and_explicit_value_wins(self):
        qt_app()
        calls = []

        def find():
            calls.append(1)
            return None

        services = AppServices.detached()
        services.find_ytdlp = find
        cache = VersionCache(services)
        cache.request()
        cache.join(5)
        cache.request()
        cache.join(5)
        self.assertEqual(cache.value, NOT_INSTALLED)
        self.assertEqual(len(calls), 1)
        cache.set(TAG)
        self.assertEqual(cache.value, TAG)
        cache.set("old", only_if_unknown=True)
        self.assertEqual(cache.value, TAG)


if __name__ == "__main__":
    unittest.main()

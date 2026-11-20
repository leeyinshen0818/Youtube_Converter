"""Everything the Settings and Update dialogs touch outside the window:
settings file, app data folder, tools folder, yt-dlp lookup, network.

``AppServices.for_app()`` is the real one (used by ``ui.app.main``).
``AppServices.detached()`` keeps settings in memory, finds no yt-dlp and has
no network; it is the default for a bare ``MainWindow()`` so tests never
reach the real ``%LOCALAPPDATA%`` folder, the real yt-dlp.exe or the network.
"""

from __future__ import annotations

import os
import tempfile
import threading
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from core import tools, updater
from core.settings import SettingsStore, settings_path


class _OfflineOpener:
    def open(self, request, timeout=None):  # noqa: ARG002
        raise urllib.error.URLError("no network in this configuration")


@dataclass
class AppServices:
    settings: SettingsStore
    app_data: Path  # holds the imported cookies.txt
    app_dir: Path  # folder of the app (old cookies.txt fallback)
    tools_dir: Path  # receives updated yt-dlp.exe
    find_ytdlp: Callable[[], Path | None]
    make_opener: Callable[[], updater.Opener] = updater.default_opener
    command_for: Callable[[Path], list[str]] = updater.default_command
    version_timeout: float = updater.VERSION_TIMEOUT_S
    replace: Callable = field(default=os.replace)

    @classmethod
    def for_app(cls, env: tools.ToolEnvironment | None = None) -> "AppServices":
        env = env or tools.default_environment()
        return cls(
            settings=SettingsStore(settings_path(env)),
            app_data=tools.app_data_dir(env),
            app_dir=env.app_dir,
            tools_dir=tools.user_tools_dir(env),
            find_ytdlp=lambda: tools.find_ytdlp(env),
        )

    @classmethod
    def detached(cls, save_dir: Path | None = None) -> "AppServices":
        # A private folder that is only created if cookies are imported.
        root = Path(tempfile.gettempdir()) / f"ytmp3-detached-{os.getpid()}-{id(object())}"
        return cls(
            settings=SettingsStore.in_memory(save_dir),
            app_data=root / "appdata",
            app_dir=root / "app",
            tools_dir=root / "appdata" / tools.TOOLS_SUBFOLDER,
            find_ytdlp=lambda: None,
            make_opener=_OfflineOpener,
        )


NOT_INSTALLED = "not installed"
UNKNOWN_VERSION = "unknown"


class VersionCache(QObject):
    """``yt-dlp --version``, looked up once per session in the background.

    ``changed`` is emitted on the GUI thread (queued) with the new text.
    """

    changed = Signal(str)

    def __init__(self, services: AppServices, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self._lock = threading.Lock()
        self._value: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def value(self) -> str | None:
        """Version text, NOT_INSTALLED, UNKNOWN_VERSION, or None while unknown."""
        with self._lock:
            return self._value

    def set(self, value: str, only_if_unknown: bool = False) -> None:
        with self._lock:
            if only_if_unknown and self._value is not None:
                return
            self._value = value
        try:
            self.changed.emit(value)
        except RuntimeError:  # the window is already gone
            pass

    def request(self) -> None:
        """Start the lookup unless the value is known or being looked up."""
        with self._lock:
            if self._value is not None or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._lookup, name="ytdlp-version", daemon=True)
            thread = self._thread
        thread.start()

    def _lookup(self) -> None:
        services = self.services
        try:
            exe = services.find_ytdlp()
        except OSError:
            exe = None
        if exe is None:
            self.set(NOT_INSTALLED, only_if_unknown=True)
            return
        version = updater.read_version(exe, services.command_for, services.version_timeout)
        # An update that finished meanwhile already knows the newer version.
        self.set(version or UNKNOWN_VERSION, only_if_unknown=True)

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

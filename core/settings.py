"""User settings stored as ``settings.json`` in the app data folder.

Pure file logic (no UI). A missing, unreadable or corrupt file never stops
the app: every value that cannot be read falls back to its default.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from core import tools
from core.saver import default_save_dir

SETTINGS_FILE = "settings.json"
MAX_SETTINGS_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    """``custom_save_dir`` is None while the default folder is used, so the
    default keeps following the user's Downloads folder."""

    custom_save_dir: Path | None = None

    @property
    def save_dir(self) -> Path:
        return self.custom_save_dir or default_save_dir()

    @property
    def uses_default_save_dir(self) -> bool:
        return self.custom_save_dir is None


def settings_path(env: tools.ToolEnvironment) -> Path:
    return tools.app_data_dir(env) / SETTINGS_FILE


def _path_value(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    return path if path.is_absolute() else None


def load_settings(path: Path) -> Settings:
    """Read ``path``; anything missing or invalid becomes the default."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) > MAX_SETTINGS_BYTES:
            return Settings()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    return Settings(custom_save_dir=_path_value(data.get("save_dir")))


def save_settings(path: Path, settings: Settings) -> None:
    """Write atomically (temp file + replace). Raises OSError on failure."""
    data = {"save_dir": str(settings.custom_save_dir) if settings.custom_save_dir else None}
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix="settings-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class SettingsStore:
    """Current settings plus where they are saved.

    ``path=None`` keeps everything in memory (used when no app data folder
    is configured, e.g. in tests).
    """

    def __init__(self, path: Path | None, settings: Settings | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        if settings is None:
            settings = load_settings(path) if path is not None else Settings()
        self._settings = settings

    @classmethod
    def in_memory(cls, save_dir: Path | None = None) -> "SettingsStore":
        return cls(None, Settings(custom_save_dir=Path(save_dir) if save_dir else None))

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    @property
    def save_dir(self) -> Path:
        return self.settings.save_dir

    def set_save_dir(self, folder: Path | None) -> None:
        """Use ``folder`` (None = the default) right away, then persist it.

        The new value stays in effect even if writing the file fails
        (OSError is raised so the caller can tell the user).
        """
        with self._lock:
            self._settings = replace(self._settings, custom_save_dir=Path(folder) if folder else None)
            current = self._settings
        if self.path is not None:
            save_settings(self.path, current)

    def reset_save_dir(self) -> None:
        self.set_save_dir(None)


def check_writable(folder: Path) -> bool:
    """True if a file can be created (and deleted) inside ``folder``.

    The folder is created if it does not exist yet.
    """
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=folder, prefix=".write-test-", suffix=".tmp")
        os.close(handle)
        os.unlink(name)
        return True
    except OSError:
        return False

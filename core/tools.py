"""Locate the external programs the converter needs (Windows only).

All filesystem and PATH access goes through a ``ToolEnvironment`` so tests
can point the lookup at temporary folders instead of the real system.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Same folder name as the old app, so an existing cookies.txt keeps working.
APP_DATA_FOLDER = "YouTube MP3 Converter"
TOOLS_SUBFOLDER = "tools"
TEMP_SUBFOLDER = "tmp"
READY_SUBFOLDER = "ready"
COOKIES_FILE = "cookies.txt"
BUNDLED_BIN = "bin"


@dataclass(frozen=True)
class ToolEnvironment:
    """Where to look for tools.

    bundle_dir: folder that holds the bundled ``bin`` folder
        (PyInstaller's ``sys._MEIPASS`` when frozen, the source folder otherwise).
    app_dir: folder that holds the app itself (the exe when frozen).
    local_app_data: ``%LOCALAPPDATA%``.
    home: the user's home folder (used for the WinGet links folder).
    which: PATH lookup, ``shutil.which`` by default.
    """

    bundle_dir: Path
    app_dir: Path
    local_app_data: Path
    home: Path
    which: Callable[[str], str | None] = field(default=shutil.which)


@dataclass(frozen=True)
class ToolSet:
    ffmpeg: Path | None
    ffprobe: Path | None
    node: Path | None
    ytdlp: Path | None

    @property
    def missing(self) -> list[str]:
        return [
            name
            for name, path in (
                ("ffmpeg", self.ffmpeg),
                ("ffprobe", self.ffprobe),
                ("node", self.node),
                ("yt-dlp", self.ytdlp),
            )
            if path is None
        ]


def default_environment() -> ToolEnvironment:
    source_dir = Path(__file__).resolve().parent.parent
    frozen = bool(getattr(sys, "frozen", False))
    app_dir = Path(sys.executable).resolve().parent if frozen else source_dir
    bundle_dir = Path(getattr(sys, "_MEIPASS", app_dir))
    home = Path.home()
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    return ToolEnvironment(
        bundle_dir=bundle_dir,
        app_dir=app_dir,
        local_app_data=local_app_data,
        home=home,
    )


def app_data_dir(env: ToolEnvironment) -> Path:
    return env.local_app_data / APP_DATA_FOLDER


def user_tools_dir(env: ToolEnvironment) -> Path:
    """Folder that receives updated copies of yt-dlp.exe."""
    return app_data_dir(env) / TOOLS_SUBFOLDER


def ready_root(env: ToolEnvironment) -> Path:
    """Staging folder: converted MP3s wait here until the user saves them."""
    return app_data_dir(env) / READY_SUBFOLDER


def temp_root(env: ToolEnvironment) -> Path:
    """Parent folder for per-job work folders."""
    return app_data_dir(env) / TEMP_SUBFOLDER


def find_cookies(env: ToolEnvironment) -> Path | None:
    """cookies.txt in the app data folder, else next to the app (old behavior)."""
    return _first_file([app_data_dir(env) / COOKIES_FILE, env.app_dir / COOKIES_FILE])


def winget_links_dir(env: ToolEnvironment) -> Path:
    return env.home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"


def _is_file(path: Path) -> bool:
    # A broken link or an unreadable folder counts as "not found" rather than
    # "found": the old app returned the folder on OSError, which made yt-dlp
    # fail later with a less helpful message.
    try:
        return path.is_file()
    except OSError:
        return False


def _first_file(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if _is_file(candidate):
            return candidate
    return None


def _on_path(env: ToolEnvironment, name: str) -> Path | None:
    found = env.which(name)
    if not found:
        return None
    path = Path(found)
    return path if _is_file(path) else None


def _find_media_tool(env: ToolEnvironment, name: str) -> Path | None:
    """Bundled copy, then PATH, then the WinGet links folder (same as app.py)."""
    exe = f"{name}.exe"
    return (
        _first_file([env.bundle_dir / BUNDLED_BIN / exe])
        or _on_path(env, name)
        or _first_file([winget_links_dir(env) / exe])
    )


def find_ffmpeg(env: ToolEnvironment) -> Path | None:
    return _find_media_tool(env, "ffmpeg")


def find_ffprobe(env: ToolEnvironment) -> Path | None:
    return _find_media_tool(env, "ffprobe")


def find_node(env: ToolEnvironment) -> Path | None:
    """Bundled copy, then PATH (same as app.py)."""
    return _first_file([env.bundle_dir / BUNDLED_BIN / "node.exe"]) or _on_path(env, "node")


def bundled_ytdlp_candidates(env: ToolEnvironment) -> list[Path]:
    return [
        env.app_dir / "yt-dlp.exe",
        env.app_dir / BUNDLED_BIN / "yt-dlp.exe",
        env.bundle_dir / BUNDLED_BIN / "yt-dlp.exe",
    ]


def find_ytdlp(env: ToolEnvironment) -> Path | None:
    """Updated copy first, then the copy shipped with the app, then PATH.

    The PATH fallback only matters when running from source.
    """
    candidates = [user_tools_dir(env) / "yt-dlp.exe", *bundled_ytdlp_candidates(env)]
    return _first_file(candidates) or _on_path(env, "yt-dlp")


def locate_tools(env: ToolEnvironment | None = None) -> ToolSet:
    env = env or default_environment()
    return ToolSet(
        ffmpeg=find_ffmpeg(env),
        ffprobe=find_ffprobe(env),
        node=find_node(env),
        ytdlp=find_ytdlp(env),
    )

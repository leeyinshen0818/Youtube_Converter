"""Texts and small decisions for the Settings and Update dialogs, kept free
of Qt so they are easy to test."""

from __future__ import annotations

from dataclasses import dataclass

from core.cookies import CookieStatus
from core.updater import Stage, UpdateKind, UpdateResult
from core.version import APP_VERSION

UPDATE_LINK_TEXT = "Downloads not working? Update"
SETTINGS_BUTTON_TEXT = "Settings"
COOKIES_HELP = "Only needed if YouTube asks you to sign in. This file contains login information; keep it private."
NOT_WRITABLE_TEXT = "Can't save files in this folder. Choose another folder."
SETTINGS_NOT_SAVED_TEXT = "Couldn't remember this choice. It is used until you close the app."
BLOCKED_TEXT = "Please wait until the current downloads finish."
UPDATE_INTRO = "If downloads stopped working, updating the download component often fixes it."
CANCELLING_TEXT = "Cancelling..."

_COOKIE_STATUS = {
    CookieStatus.IMPORTED: "Cookies: imported",
    CookieStatus.NOT_IMPORTED: "Cookies: not imported",
    CookieStatus.NEXT_TO_APP: "Cookies: found next to the app",
}

_STAGE_TEXT = {
    Stage.CHECKING: "Checking for a new version...",
    Stage.DOWNLOADING: "Downloading the new version...",
    Stage.VERIFYING: "Checking the download...",
    Stage.INSTALLING: "Installing...",
}


def cookie_status_text(status: CookieStatus) -> str:
    return _COOKIE_STATUS[status]


def app_version_text(version: str = APP_VERSION) -> str:
    return f"App {version}"


def component_version_text(version: str | None) -> str:
    return f"Download component (yt-dlp): {version or 'checking...'}"


def current_version_text(version: str | None) -> str:
    return f"Current version: {version or 'checking...'}"


def stage_text(stage: Stage) -> str:
    return _STAGE_TEXT[stage]


@dataclass(frozen=True)
class ProgressState:
    """QProgressBar range and value; maximum 0 means "busy" (no percentage)."""

    maximum: int
    value: int


def progress_state(stage: Stage, done: int, total: int | None) -> ProgressState:
    if stage is not Stage.DOWNLOADING or not total or total <= 0:
        return ProgressState(0, 0)
    return ProgressState(100, max(0, min(100, int(done * 100 / total))))


@dataclass(frozen=True)
class ResultText:
    text: str
    role: str  # QLabel role: done | warn | error | message
    tooltip: str  # raw details, only ever shown as a tooltip


def result_text(result: UpdateResult) -> ResultText:
    if result.kind is UpdateKind.UPDATED:
        return ResultText(f"Updated to {result.version}. You can try again now.", "done", "")
    if result.kind is UpdateKind.INSTALLED:
        return ResultText(f"Installed version {result.version}. You can try again now.", "done", "")
    if result.kind is UpdateKind.ALREADY_LATEST:
        return ResultText(f"You already have the latest version ({result.version}).", "done", "")
    if result.kind is UpdateKind.CANCELLED:
        return ResultText("Update cancelled. Nothing was changed.", "message", "")
    return ResultText(result.message or "Couldn't update. Your current version still works.", "error", result.details)

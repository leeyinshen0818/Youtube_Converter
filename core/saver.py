"""Move finished MP3s from the staging folder to the user's save folder.

Pure file logic (no UI). Call from a worker thread: copying across drives
can take a moment.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

from core.errors import ErrorKind, FriendlyError, classify_error

ERROR_NOT_SAME_DEVICE = 17  # Windows: "The system cannot move the file to a different disk drive."


def default_save_dir() -> Path:
    from core.converter import default_output_dir

    return default_output_dir()


def claim_name(dest_dir: Path, base_name: str, suffix: str) -> Path:
    """Create an empty file with a free name: "x.mp3", "x (2).mp3", ...

    Creating the file exclusively claims the name, so two saves of the same
    title can never pick the same file.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    counter = 1
    while True:
        name = base_name if counter == 1 else f"{base_name} ({counter})"
        candidate = dest_dir / f"{name}{suffix}"
        try:
            with open(candidate, "xb"):
                return candidate
        except FileExistsError:
            counter += 1


def is_cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == ERROR_NOT_SAME_DEVICE


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def move_file(source: Path, dest_dir: Path, base_name: str) -> Path:
    """Move ``source`` into ``dest_dir`` as ``base_name`` + its suffix.

    Same drive: an atomic rename over the claimed name. Other drive: copy over
    the claimed name, then delete the source. On any failure the claimed file
    is removed and ``source`` is left untouched.
    """
    target = claim_name(dest_dir, base_name, source.suffix)
    try:
        try:
            os.replace(source, target)
            return target
        except OSError as exc:
            if not is_cross_device(exc):
                raise
        shutil.copyfile(source, target)
    except BaseException:
        _discard(target)
        raise
    _discard(source)  # the copy is complete; a leftover is cleaned at next start
    return target


def save_item(staged_path: Path, dest_dir: Path) -> Path:
    """Save a staged MP3 into ``dest_dir`` with a Windows-safe, unique name.

    Raises FileNotFoundError if the staged file is gone, OSError otherwise.
    """
    from core.converter import sanitize_filename

    staged_path = Path(staged_path)
    if not staged_path.is_file():
        raise FileNotFoundError(f"The staged file no longer exists: {staged_path}")
    base_name = sanitize_filename(staged_path.stem, fallback="audio")
    return move_file(staged_path, Path(dest_dir), base_name)


def remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def discard_staged(staged_path: Path | None) -> None:
    """Delete a staged MP3 and its (now empty) per-job folder."""
    if staged_path is None:
        return
    staged_path = Path(staged_path)
    _discard(staged_path)
    remove_empty_dir(staged_path.parent)


def missing_file_error(details: str = "") -> FriendlyError:
    return FriendlyError(
        kind=ErrorKind.FILE_MISSING,
        message="File is no longer available",
        hint="Press Retry to convert it again.",
        details=details,
    )


def save_error(exc: BaseException) -> FriendlyError:
    """Friendly message for a failed save (disk full, no permission, ...)."""
    if isinstance(exc, FileNotFoundError):
        return missing_file_error(str(exc))
    error = classify_error(str(exc))
    if error.kind in (ErrorKind.DISK_FULL, ErrorKind.NO_PERMISSION):
        return error
    return FriendlyError(
        kind=ErrorKind.UNKNOWN,
        message="Couldn't save the MP3",
        hint="Press Download to try again.",
        details=error.details,
    )

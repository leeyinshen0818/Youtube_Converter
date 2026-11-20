"""Allow one running copy of the app per Windows session (no UI, no Qt).

A named mutex is used instead of a lock file: Windows destroys it as soon as
the last handle closes, which happens on a normal exit and also when the
process crashes or is killed, so a stale lock can never block the next start.
The ``Local\\`` prefix makes it per signed-in user session.
"""

from __future__ import annotations

import ctypes
import sys

APP_MUTEX_NAME = "Local\\YouTubeMP3Converter.SingleInstance"
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """``acquire()`` is True for the first holder of ``name``; the guard is
    released by ``release()`` or when the process ends in any way."""

    def __init__(self, name: str = APP_MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if sys.platform != "win32":  # the app is Windows-only; never block elsewhere
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            # Could not create or open it at all (should not happen): do not
            # lock the user out of the app because of the guard.
            return True
        if error == ERROR_ALREADY_EXISTS:
            _close(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            _close(handle)

    @property
    def held(self) -> bool:
        return self._handle is not None


def _close(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(handle)

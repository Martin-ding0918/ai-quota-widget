"""Windows named-mutex guard for a single widget instance."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\AIQuotaWidget-6F1D5A0E4C2B"

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - the widget targets Windows
    _kernel32 = None


class SingleInstance:
    """Hold a named mutex for the lifetime of the first process."""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self.already_running = False
        self._handle = None
        if _kernel32 is None:
            return
        handle = _kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        self._handle = handle
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

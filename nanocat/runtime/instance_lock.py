"""Cross-platform exclusive ownership for one NanoCat workdir."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

_LOCK_FILE_BYTES = 64


def _lock_is_contended(exc: OSError) -> bool:
    """Return whether an OS error specifically reports lock contention."""
    if os.name == "nt":
        return getattr(exc, "winerror", None) in {32, 33} or exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
        }
    return exc.errno in {errno.EACCES, errno.EAGAIN}


class RuntimeInstanceLock:
    """Hold an advisory one-byte lock for the lifetime of a runtime."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        try:
            if os.fstat(handle.fileno()).st_size < _LOCK_FILE_BYTES:
                handle.seek(0)
                handle.write(b"\0" * _LOCK_FILE_BYTES)
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if _lock_is_contended(exc):
                    raise RuntimeError(
                        "NanoCat workdir is already owned by another process: "
                        f"{self.path.parent}"
                    ) from exc
                raise
            handle.seek(1)
            pid = f"{os.getpid():<31}".encode("ascii")
            handle.write(pid)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

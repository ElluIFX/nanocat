"""Build and locate the bundled NanoCat web client."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

_IGNORED_PARTS = frozenset(
    {
        "dist",
        "e2e",
        "node_modules",
        ".git",
        ".nanocat-build.lock",
        "coverage",
        "playwright-report",
        "test-results",
    }
)
_BUILD_LOCK_WAIT_S = 1300.0


@contextmanager
def _frontend_build_lock(frontend: Path) -> Iterator[None]:
    """Serialize dependency installation and builds across NanoCat processes."""
    lock_path = frontend / ".nanocat-build.lock"
    handle: BinaryIO = lock_path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + _BUILD_LOCK_WAIT_S
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for the frontend build lock") from None
                    time.sleep(0.1)
        else:
            import fcntl

            deadline = time.monotonic() + _BUILD_LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for the frontend build lock") from None
                    time.sleep(0.1)
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _frontend_root() -> Path | None:
    root = Path(__file__).resolve().parents[2] / "frontend"
    return root if (root / "package.json").is_file() else None


def _packaged_assets() -> Path:
    return Path(__file__).resolve().parents[1] / "web" / "static"


def _source_fingerprint(frontend: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(frontend.rglob("*")):
        if (
            not path.is_file()
            or any(part in _IGNORED_PARTS for part in path.parts)
            or ".test." in path.name
        ):
            continue
        relative = path.relative_to(frontend).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _read_stamp(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_stamp(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _run_npm(frontend: Path, *args: str) -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "The NanoCat web client needs Node.js and npm for a source checkout. "
            "Install Node.js or use a built wheel."
        )
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen([npm, *args], cwd=frontend, **popen_kwargs)
    try:
        returncode = process.wait(timeout=600.0)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise RuntimeError(f"npm {' '.join(args)} timed out after 600 seconds") from exc
    if returncode:
        raise RuntimeError(f"npm {' '.join(args)} failed with exit code {returncode}")


def ensure_web_assets() -> Path:
    """Return ready static assets, rebuilding a source checkout when required."""
    frontend = _frontend_root()
    packaged = _packaged_assets()
    if frontend is None:
        if (packaged / "index.html").is_file():
            return packaged
        raise RuntimeError("NanoCat web assets are missing from this installation")

    with _frontend_build_lock(frontend):
        dist = frontend / "dist"
        source_hash = _source_fingerprint(frontend)
        build_stamp = dist / ".nanocat-build.json"
        if (
            (dist / "index.html").is_file()
            and _read_stamp(build_stamp).get("source") == source_hash
        ):
            return dist

        lock_path = frontend / "package-lock.json"
        if not lock_path.is_file():
            raise RuntimeError("frontend/package-lock.json is required for a reproducible build")
        lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        dependency_stamp = frontend / "node_modules" / ".nanocat-lock"
        if _read_stamp(dependency_stamp).get("lock") != lock_hash:
            _run_npm(frontend, "ci", "--no-audit", "--no-fund")
            _write_stamp(dependency_stamp, {"lock": lock_hash})
        _run_npm(frontend, "run", "build")
        if not (dist / "index.html").is_file():
            raise RuntimeError("frontend build completed without dist/index.html")
        _write_stamp(build_stamp, {"source": source_hash})
        return dist


def locate_web_assets() -> Path | None:
    """Return an existing source or installed asset directory without building it."""
    frontend = _frontend_root()
    source_dist = frontend / "dist" if frontend is not None else None
    if source_dist is not None and (source_dist / "index.html").is_file():
        return source_dist
    packaged = _packaged_assets()
    return packaged if (packaged / "index.html").is_file() else None

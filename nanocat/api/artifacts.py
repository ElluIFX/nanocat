"""Opaque, quota-bound media and artifact registry for HTTP clients."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable, TypeVar
from uuid import uuid4

from fastapi import UploadFile
from loguru import logger

_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_T = TypeVar("_T")
_OWNER_CANCEL_TIMEOUT_S = 15.0
_CLOSE_LOCK_TIMEOUT_S = 2.0


async def _await_owned(awaitable: Awaitable[_T]) -> _T:
    """Drain one owned operation before propagating caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        deadline = asyncio.get_running_loop().time() + _OWNER_CANCEL_TIMEOUT_S
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                task.cancel()
                logger.error("Artifact owner operation exceeded its cancellation deadline")
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.CancelledError:
                continue
            except TimeoutError:
                task.cancel()
                logger.error("Artifact owner operation exceeded its cancellation deadline")
                break
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
        raise cancelled


async def _await_owned_bounded(awaitable: Awaitable[_T]) -> _T:
    """Bound cleanup I/O even after the caller cancellation was consumed."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_OWNER_CANCEL_TIMEOUT_S,
        )
    except (asyncio.CancelledError, TimeoutError):
        task.cancel()
        logger.error("Artifact owner operation exceeded its hard deadline")
        raise


async def _run_daemon_thread(
    operation: Callable[[], _T],
    *,
    name: str,
    on_detached: Callable[[_T], None] | None = None,
    on_detached_settle: Callable[[], None] | None = None,
) -> _T:
    """Run blocking publication outside asyncio's shutdown-owned executor."""
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[_T] = loop.create_future()

    def settle_detached(
        result: _T | None = None,
        error: BaseException | None = None,
    ) -> None:
        if on_detached_settle is not None:
            threading.Thread(
                target=on_detached_settle,
                name=f"{name}.settle-finalizer",
                daemon=True,
            ).start()
        if error is None and result is not None and on_detached is not None:
            threading.Thread(
                target=on_detached,
                args=(result,),
                name=f"{name}.settle",
                daemon=True,
            ).start()

    def deliver(result: _T | None = None, error: BaseException | None = None) -> None:
        if completion.done():
            settle_detached(result, error)
            return
        if error is not None:
            completion.set_exception(error)
        else:
            completion.set_result(result)  # type: ignore[arg-type]

    def run() -> None:
        try:
            result = operation()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(deliver, None, exc)
            except RuntimeError:
                settle_detached(None, exc)
        else:
            try:
                loop.call_soon_threadsafe(deliver, result, None)
            except RuntimeError:
                settle_detached(result, None)

    threading.Thread(target=run, name=name, daemon=True).start()
    try:
        return await completion
    except asyncio.CancelledError:
        if completion.done() and not completion.cancelled():
            try:
                detached_result = completion.result()
            except BaseException as exc:
                settle_detached(None, exc)
            else:
                settle_detached(detached_result, None)
        elif not completion.done():
            completion.cancel()
        raise


@dataclass(slots=True)
class ArtifactEntry:
    id: str
    filename: str
    name: str
    size: int
    media_type: str
    kind: str
    created_at: float
    last_access: float
    owned: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "mediaType": self.media_type,
            "kind": self.kind,
        }


@dataclass(slots=True)
class _PublishReceipt:
    entry: ArtifactEntry
    victims: tuple[ArtifactEntry, ...]


def _kind(media_type: str, name: str) -> str:
    suffix = Path(name).suffix.casefold()
    if media_type.startswith("image/"):
        return "image"
    if media_type in {"application/json", "application/problem+json"} or suffix == ".json":
        return "json"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".diff", ".patch"}:
        return "diff"
    if media_type.startswith("text/"):
        return "text"
    return "binary"


def _safe_media_type(value: str | None, name: str) -> str:
    candidate = str(value or "").split(";", 1)[0].strip().casefold()
    if _MEDIA_TYPE_RE.fullmatch(candidate):
        return candidate
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class ArtifactRegistry:
    """Persist opaque file mappings while enforcing bounded owned storage."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_total_bytes: int = 256 * 1024 * 1024,
        max_entries: int = 4096,
        max_concurrent_uploads: int = 4,
    ) -> None:
        if (
            not 0 < max_file_bytes <= max_total_bytes
            or max_entries <= 0
            or max_concurrent_uploads <= 0
        ):
            raise ValueError("invalid artifact registry limits")
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self._manifest = self.root / "registry.json"
        self._entries: OrderedDict[str, ArtifactEntry] = OrderedDict()
        self._pending_deletes: OrderedDict[str, int] = OrderedDict()
        self._leases: dict[str, tuple[str, ...]] = {}
        self._pins: Counter[str] = Counter()
        self._pending_publications: dict[str, _PublishReceipt] = {}
        self._protected_pending_files: Counter[str] = Counter()
        self._lock = threading.RLock()
        self._lease_lock = threading.Lock()
        self._upload_slots = asyncio.Semaphore(max_concurrent_uploads)
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._degraded = False
        self._load()

    def _load(self) -> None:
        manifest_exists = self._manifest.exists()
        if manifest_exists:
            try:
                raw = json.loads(self._manifest.read_text(encoding="utf-8"))
                if (
                    not isinstance(raw, dict)
                    or not isinstance(raw.get("items"), list)
                    or not isinstance(raw.get("pendingDeletes", []), list)
                ):
                    raise ValueError("artifact manifest schema is invalid")
            except (OSError, UnicodeError, ValueError):
                self._degraded = True
                logger.error(
                    "Artifact manifest is unreadable; preserving registry files in degraded mode"
                )
                return
        else:
            raw = {"version": 2, "items": [], "pendingDeletes": []}
        self._cleanup_crash_temps_locked()
        items = raw.get("items", [])
        if len(items) > self.max_entries:
            self._degraded = True
            logger.error(
                "Artifact manifest exceeds the configured entry limit; preserving files"
            )
            return
        parsed_entries: list[ArtifactEntry] = []
        seen_ids: set[str] = set()
        for value in items:
            try:
                if not isinstance(value, dict):
                    raise ValueError("artifact entry is not an object")
                entry = ArtifactEntry(**value)
                if (
                    not re.fullmatch(r"[0-9a-f]{32}", entry.id)
                    or entry.id in seen_ids
                    or not isinstance(entry.filename, str)
                    or not isinstance(entry.name, str)
                    or not isinstance(entry.size, int)
                    or isinstance(entry.size, bool)
                    or not 0 <= entry.size <= self.max_file_bytes
                    or not isinstance(entry.media_type, str)
                    or not isinstance(entry.kind, str)
                    or not isinstance(entry.created_at, (int, float))
                    or not isinstance(entry.last_access, (int, float))
                    or not isinstance(entry.owned, bool)
                    or (entry.owned and entry.filename != f"{entry.id}.bin")
                ):
                    raise ValueError("artifact entry fields are invalid")
                path = self._path(entry) if entry.owned else Path(entry.filename).resolve()
                if not path.is_file() or path.stat().st_size != entry.size:
                    raise ValueError("artifact entry file is unavailable or changed")
            except (OSError, TypeError, ValueError):
                self._degraded = True
                logger.error(
                    "Artifact manifest contains an invalid entry; preserving files in degraded mode"
                )
                return
            parsed_entries.append(entry)
            seen_ids.add(entry.id)
        self._entries.update((entry.id, entry) for entry in parsed_entries)
        pending = raw.get("pendingDeletes", [])
        for value in pending:
            if not isinstance(value, dict):
                self._degraded = True
                logger.error("Artifact manifest has invalid pending deletes; preserving files")
                return
            filename = value.get("filename")
            size = value.get("size")
            if (
                not isinstance(filename, str)
                or not re.fullmatch(r"[0-9a-f]{32}\.bin", filename)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                self._degraded = True
                logger.error("Artifact manifest has invalid pending deletes; preserving files")
                return
            self._pending_deletes[filename] = size
        referenced = {
            entry.filename for entry in self._entries.values() if entry.owned
        }
        for candidate in self.root.glob("*.bin"):
            if (
                re.fullmatch(r"[0-9a-f]{32}\.bin", candidate.name)
                and candidate.name not in referenced
            ):
                try:
                    self._pending_deletes[candidate.name] = candidate.stat().st_size
                except OSError:
                    continue
        self._reap_pending_locked()
        try:
            self._save_locked()
        except OSError:
            pass

    def _save_entries_locked(
        self,
        entries: OrderedDict[str, ArtifactEntry],
        pending_deletes: OrderedDict[str, int] | None = None,
    ) -> None:
        if self._degraded:
            raise RuntimeError("artifact registry is degraded and read-only")
        pending = self._pending_deletes if pending_deletes is None else pending_deletes
        payload = json.dumps(
            {
                "version": 2,
                "items": [asdict(entry) for entry in entries.values()],
                "pendingDeletes": [
                    {"filename": filename, "size": size}
                    for filename, size in pending.items()
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=".registry.", suffix=".tmp", dir=self.root, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._manifest)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _save_locked(self) -> None:
        self._save_entries_locked(self._entries)

    def _path(self, entry: ArtifactEntry) -> Path:
        path = (self.root / entry.filename).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("artifact registry path escaped its root")
        return path

    def _eviction_plan_locked(self, incoming: int) -> list[str]:
        """Plan capacity reclamation without mutating the live registry."""
        self._reap_pending_locked()
        owned_total = self._physical_owned_bytes_locked()
        if owned_total > self.max_total_bytes:
            raise RuntimeError("artifact registry is awaiting physical quota reclamation")
        with self._lease_lock:
            pinned_ids = {artifact_id for artifact_id, count in self._pins.items() if count > 0}
        remaining_count = len(self._entries)
        victims: list[str] = []
        for artifact_id, entry in self._entries.items():
            if (
                remaining_count < self.max_entries
                and owned_total + incoming <= self.max_total_bytes
            ):
                break
            if artifact_id in pinned_ids:
                continue
            victims.append(artifact_id)
            remaining_count -= 1
            if entry.owned:
                owned_total -= entry.size
        if (
            remaining_count >= self.max_entries
            or owned_total + incoming > self.max_total_bytes
        ):
            raise RuntimeError("artifact registry capacity is leased")
        return victims

    def _physical_owned_bytes_locked(self) -> int:
        total = 0
        for candidate in self.root.iterdir():
            if not (
                re.fullmatch(r"[0-9a-f]{32}\.bin", candidate.name)
                or re.fullmatch(r"\.[0-9a-f]{32}\.[A-Za-z0-9_-]+\.tmp", candidate.name)
                or re.fullmatch(r"\.registry\.[A-Za-z0-9_-]+\.tmp", candidate.name)
            ):
                continue
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def _reserve_staging_bytes_locked(self, incoming: int) -> None:
        """Reclaim LRU payloads before one upload chunk consumes physical space."""
        if incoming <= 0:
            return
        self._reap_pending_locked()
        owned_total = self._physical_owned_bytes_locked()
        if owned_total > self.max_total_bytes:
            raise RuntimeError("artifact registry is awaiting physical quota reclamation")
        with self._lease_lock:
            pinned_ids = {artifact_id for artifact_id, count in self._pins.items() if count > 0}
        victims: list[str] = []
        for artifact_id, entry in self._entries.items():
            if owned_total + incoming <= self.max_total_bytes:
                break
            if artifact_id in pinned_ids:
                continue
            victims.append(artifact_id)
            if entry.owned:
                owned_total -= entry.size
        if owned_total + incoming > self.max_total_bytes:
            raise RuntimeError("artifact registry capacity is leased")
        if not victims:
            return
        candidate, pending = self._eviction_candidate_locked(victims)
        self._save_entries_locked(candidate, pending)
        self._entries = candidate
        self._pending_deletes = pending
        self._reap_pending_locked()
        if self._physical_owned_bytes_locked() + incoming > self.max_total_bytes:
            raise RuntimeError("artifact registry is awaiting physical quota reclamation")

    def _write_upload_chunk(self, handle: BinaryIO, chunk: bytes) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("artifact registry is closed")
            if self._degraded:
                raise RuntimeError("artifact registry is degraded and read-only")
            self._reserve_staging_bytes_locked(len(chunk))
            view = memoryview(chunk)
            written_total = 0
            while written_total < len(view):
                written = handle.write(view[written_total:])
                if written is None or written <= 0:
                    raise OSError("artifact upload write made no progress")
                written_total += written
            return written_total

    def _cleanup_crash_temps_locked(self) -> None:
        for candidate in self.root.iterdir():
            if not (
                re.fullmatch(r"\.[0-9a-f]{32}\.[A-Za-z0-9_-]+\.tmp", candidate.name)
                or re.fullmatch(r"\.registry\.[A-Za-z0-9_-]+\.tmp", candidate.name)
            ):
                continue
            try:
                candidate.unlink()
            except OSError:
                pass

    def _reap_pending_locked(self) -> None:
        for filename in tuple(self._pending_deletes):
            if self._protected_pending_files.get(filename, 0) > 0:
                continue
            path = (self.root / filename).resolve()
            if self.root not in path.parents:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._pending_deletes.pop(filename, None)

    def _eviction_candidate_locked(
        self,
        victims: list[str],
    ) -> tuple[OrderedDict[str, ArtifactEntry], OrderedDict[str, int]]:
        """Build one publish transaction without mutating live registry state."""
        candidate = OrderedDict(
            (artifact_id, entry)
            for artifact_id, entry in self._entries.items()
            if artifact_id not in victims
        )
        pending = OrderedDict(self._pending_deletes)
        for artifact_id in victims:
            entry = self._entries[artifact_id]
            if entry.owned:
                pending[entry.filename] = entry.size
        return candidate, pending

    async def upload(self, upload: UploadFile) -> ArtifactEntry:
        """Stream one upload to a temporary file, then atomically publish it."""
        async with self._upload_slots:
            return await self._upload(upload)

    async def _upload(self, upload: UploadFile) -> ArtifactEntry:
        artifact_id = uuid4().hex
        safe_name = Path(upload.filename or "upload.bin").name[:255] or "upload.bin"
        media_type = _safe_media_type(upload.content_type, safe_name)
        fd, temp_name = tempfile.mkstemp(prefix=f".{artifact_id}.", suffix=".tmp", dir=self.root)
        size = 0
        handle = os.fdopen(fd, "wb", buffering=0)

        def cleanup_detached() -> None:
            self._remove_temp_file(temp_name)

        receipt: _PublishReceipt | None = None
        upload_closed = False
        try:
            try:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise ValueError("uploaded file exceeds the per-file limit")
                    await _await_owned(
                        _run_daemon_thread(
                            lambda value=chunk: self._write_upload_chunk(handle, value),
                            name=f"nanocat.artifact-write-io.{artifact_id}",
                            on_detached_settle=cleanup_detached,
                        )
                    )
                await _await_owned(
                    _run_daemon_thread(
                        lambda: self._flush_upload(handle),
                        name=f"nanocat.artifact-flush-io.{artifact_id}",
                        on_detached_settle=cleanup_detached,
                    )
                )
            finally:
                await _await_owned(
                    _run_daemon_thread(
                        handle.close,
                        name=f"nanocat.artifact-close-io.{artifact_id}",
                        on_detached_settle=cleanup_detached,
                    )
                )
            filename = f"{artifact_id}.bin"
            now = time.time()
            entry = ArtifactEntry(
                id=artifact_id,
                filename=filename,
                name=safe_name,
                size=size,
                media_type=media_type,
                kind=_kind(media_type, safe_name),
                created_at=now,
                last_access=now,
            )
            worker = asyncio.create_task(
                _run_daemon_thread(
                    lambda: self._publish_upload(entry, temp_name, size),
                    name=f"nanocat.artifact-publish-io.{artifact_id}",
                    on_detached=self._settle_detached_publication,
                ),
                name=f"nanocat.artifact-publish.{artifact_id}",
            )
            try:
                receipt = await asyncio.shield(worker)
            except asyncio.CancelledError as cancelled:
                deadline = asyncio.get_running_loop().time() + _OWNER_CANCEL_TIMEOUT_S
                while not worker.done():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        worker.cancel()
                        logger.error(
                            "Artifact publication exceeded its cancellation deadline"
                        )
                        break
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(worker),
                            timeout=remaining,
                        )
                    except asyncio.CancelledError:
                        continue
                    except TimeoutError:
                        worker.cancel()
                        logger.error(
                            "Artifact publication exceeded its cancellation deadline"
                        )
                        break
                try:
                    if worker.done() and not worker.cancelled():
                        receipt = worker.result()
                except BaseException:
                    receipt = None
                raise cancelled
            try:
                await _await_owned_bounded(upload.close())
            finally:
                upload_closed = True
            await _await_owned_bounded(
                _run_daemon_thread(
                    lambda: self._ack_publish(receipt),
                    name=f"nanocat.artifact-ack-io.{artifact_id}",
                )
            )
            return entry
        except BaseException:
            if receipt is not None:
                try:
                    await _await_owned_bounded(
                        _run_daemon_thread(
                            lambda: self._rollback_publish(receipt),
                            name=f"nanocat.artifact-rollback-io.{artifact_id}",
                        )
                    )
                except BaseException as exc:
                    try:
                        await _await_owned_bounded(
                            _run_daemon_thread(
                                lambda: self._abandon_cancelled_publication(receipt),
                                name=f"nanocat.artifact-abandon-io.{artifact_id}",
                            )
                        )
                    except BaseException:
                        pass
                    logger.warning(
                        "Artifact publication rollback failed; committed artifact "
                        "was left eligible for bounded reclamation ({})",
                        type(exc).__name__,
                    )
            raise
        finally:
            try:
                if not upload_closed:
                    try:
                        await _await_owned_bounded(upload.close())
                    finally:
                        upload_closed = True
            finally:
                try:
                    await _await_owned_bounded(
                        _run_daemon_thread(
                            lambda: self._remove_temp_file(temp_name),
                            name=f"nanocat.artifact-temp-cleanup-io.{artifact_id}",
                        )
                    )
                except BaseException:
                    pass

    @staticmethod
    def _flush_upload(handle: BinaryIO) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _remove_temp_file(temp_name: str) -> None:
        try:
            os.unlink(temp_name)
        except OSError:
            pass

    def _publish_upload(
        self,
        entry: ArtifactEntry,
        temp_name: str,
        size: int,
    ) -> _PublishReceipt:
        with self._lock:
            if self._closed:
                raise RuntimeError("artifact registry is closed")
            if self._degraded:
                raise RuntimeError("artifact registry is degraded and read-only")
            if size > self.max_total_bytes:
                raise ValueError("uploaded file exceeds the total media quota")
            victims = self._eviction_plan_locked(0)
            victim_entries = tuple(self._entries[artifact_id] for artifact_id in victims)
            candidate_entries, pending = self._eviction_candidate_locked(victims)
            candidate_entries[entry.id] = entry
            final_path = self._path(entry)
            os.replace(temp_name, final_path)
            try:
                self._save_entries_locked(candidate_entries, pending)
            except BaseException:
                final_path.unlink(missing_ok=True)
                raise
            self._entries = candidate_entries
            self._pending_deletes = pending
            receipt = _PublishReceipt(entry=entry, victims=victim_entries)
            self._pending_publications[entry.id] = receipt
            with self._lease_lock:
                self._pins[entry.id] += 1
            for victim in victim_entries:
                if victim.owned:
                    self._protected_pending_files[victim.filename] += 1
            return receipt

    def _release_publication_guards_locked(self, receipt: _PublishReceipt) -> None:
        self._pending_publications.pop(receipt.entry.id, None)
        with self._lease_lock:
            self._pins[receipt.entry.id] -= 1
            if self._pins[receipt.entry.id] <= 0:
                self._pins.pop(receipt.entry.id, None)
        for victim in receipt.victims:
            if not victim.owned:
                continue
            self._protected_pending_files[victim.filename] -= 1
            if self._protected_pending_files[victim.filename] <= 0:
                self._protected_pending_files.pop(victim.filename, None)

    def _ack_publish(self, receipt: _PublishReceipt) -> None:
        """Make a committed upload eligible for ordinary deferred reclamation."""
        with self._lock:
            if self._pending_publications.get(receipt.entry.id) is receipt:
                self._release_publication_guards_locked(receipt)
                self._reap_pending_locked()
                try:
                    self._save_locked()
                except OSError:
                    pass

    def _abandon_cancelled_publication(self, receipt: _PublishReceipt) -> None:
        """Release guards when rollback failed without exposing an unknown ID."""
        with self._lock:
            if self._pending_publications.get(receipt.entry.id) is not receipt:
                return
            self._release_publication_guards_locked(receipt)
            self._reap_pending_locked()
            try:
                self._save_locked()
            except OSError:
                pass

    def _settle_detached_publication(self, receipt: _PublishReceipt) -> None:
        """Withdraw a publication completed after its request owner timed out."""
        try:
            self._rollback_publish(receipt)
        except BaseException as exc:
            try:
                self._abandon_cancelled_publication(receipt)
            except BaseException:
                pass
            logger.warning(
                "Detached artifact publication cleanup failed ({})",
                type(exc).__name__,
            )

    def _rollback_publish(self, receipt: _PublishReceipt) -> None:
        """Withdraw an upload whose caller was cancelled before receiving its ID."""
        with self._lock:
            if self._pending_publications.get(receipt.entry.id) is not receipt:
                return
            candidate = OrderedDict(
                (entry.id, entry) for entry in receipt.victims
            )
            candidate.update(
                (artifact_id, entry)
                for artifact_id, entry in self._entries.items()
                if artifact_id != receipt.entry.id and artifact_id not in candidate
            )
            pending = OrderedDict(self._pending_deletes)
            for victim in receipt.victims:
                if victim.owned:
                    pending.pop(victim.filename, None)
            pending[receipt.entry.filename] = receipt.entry.size
            self._save_entries_locked(candidate, pending)
            self._entries = candidate
            self._pending_deletes = pending
            self._release_publication_guards_locked(receipt)
            self._reap_pending_locked()
            try:
                self._save_locked()
            except OSError:
                pass

    def register_path(
        self,
        path: Path,
        *,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactEntry:
        """Register a trusted runtime-owned file without exposing its path."""
        resolved = path.expanduser().resolve(strict=True)
        stat = resolved.stat()
        if not resolved.is_file() or stat.st_size > self.max_file_bytes:
            raise ValueError("artifact is not a bounded regular file")
        artifact_id = uuid4().hex
        now = time.time()
        effective_media_type = _safe_media_type(media_type, resolved.name)
        entry = ArtifactEntry(
            id=artifact_id,
            filename=str(resolved),
            name=(name or resolved.name)[:255],
            size=stat.st_size,
            media_type=effective_media_type,
            kind=_kind(effective_media_type, resolved.name),
            created_at=now,
            last_access=now,
            owned=False,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("artifact registry is closed")
            if self._degraded:
                raise RuntimeError("artifact registry is degraded and read-only")
            victims = self._eviction_plan_locked(0)
            candidate_entries, pending = self._eviction_candidate_locked(victims)
            candidate_entries[entry.id] = entry
            self._save_entries_locked(candidate_entries, pending)
            self._entries = candidate_entries
            self._pending_deletes = pending
            self._reap_pending_locked()
            try:
                self._save_locked()
            except OSError:
                pass
        return entry

    def _discard_registered_path(self, entry: ArtifactEntry) -> None:
        """Remove a detached path registration without deleting the source file."""
        try:
            with self._lock:
                current = self._entries.get(entry.id)
                if current is None or current.filename != entry.filename:
                    return
                candidate = OrderedDict(
                    (artifact_id, item)
                    for artifact_id, item in self._entries.items()
                    if artifact_id != entry.id
                )
                self._save_entries_locked(candidate, self._pending_deletes)
                self._entries = candidate
        except Exception:
            logger.exception("Failed to discard detached artifact registration")

    async def register_path_async(
        self,
        path: Path,
        *,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactEntry:
        """Register a path through a cancellation-safe daemon I/O owner."""
        return await _run_daemon_thread(
            lambda: self.register_path(path, name=name, media_type=media_type),
            name="nanocat.artifact-register-path-io",
            on_detached=self._discard_registered_path,
        )

    def _resolve_locked(self, artifact_id: str) -> tuple[ArtifactEntry, Path] | None:
        entry = self._entries.get(artifact_id)
        if entry is None:
            return None
        path = Path(entry.filename).resolve() if not entry.owned else self._path(entry)
        try:
            valid = path.is_file() and path.stat().st_size == entry.size
        except OSError:
            valid = False
        if not valid:
            self._entries.pop(artifact_id, None)
            self._save_locked()
            return None
        entry.last_access = time.time()
        self._entries.move_to_end(artifact_id)
        return entry, path

    def resolve(self, artifact_id: str) -> tuple[ArtifactEntry, Path] | None:
        if len(artifact_id) != 32 or any(char not in "0123456789abcdef" for char in artifact_id):
            return None
        with self._lock:
            if self._closed:
                return None
            return self._resolve_locked(artifact_id)

    async def resolve_async(self, artifact_id: str) -> tuple[ArtifactEntry, Path] | None:
        """Resolve an artifact through a daemon I/O owner."""
        return await _run_daemon_thread(
            lambda: self.resolve(artifact_id),
            name=f"nanocat.artifact-resolve-io.{artifact_id[:12]}",
        )

    def _resolve_public_map(
        self,
        artifact_ids: set[str],
    ) -> dict[str, dict[str, Any] | None]:
        with self._lock:
            if self._closed:
                return {artifact_id: None for artifact_id in artifact_ids}
            return {
                artifact_id: (
                    resolved[0].public()
                    if (resolved := self._resolve_locked(artifact_id)) is not None
                    else None
                )
                for artifact_id in artifact_ids
            }

    async def resolve_public_map_async(
        self,
        artifact_ids: set[str],
    ) -> dict[str, dict[str, Any] | None]:
        """Resolve public metadata through one daemon I/O owner."""
        return await _run_daemon_thread(
            lambda: self._resolve_public_map(artifact_ids),
            name="nanocat.artifact-resolve-map-io",
        )

    def lease_paths(self, artifact_ids: list[str]) -> tuple[list[str], str | None]:
        """Resolve paths atomically and pin them until the returned lease is released."""
        if not artifact_ids:
            return [], None
        with self._lock:
            if self._closed:
                raise RuntimeError("artifact registry is closed")
            resolved_items: list[tuple[ArtifactEntry, Path]] = []
            for artifact_id in artifact_ids:
                if len(artifact_id) != 32 or any(
                    char not in "0123456789abcdef" for char in artifact_id
                ):
                    raise KeyError(artifact_id)
                resolved = self._resolve_locked(artifact_id)
                if resolved is None:
                    raise KeyError(artifact_id)
                resolved_items.append(resolved)
            lease_id = uuid4().hex
            pinned = tuple(dict.fromkeys(artifact_ids))
            with self._lease_lock:
                self._leases[lease_id] = pinned
                self._pins.update(pinned)
            return [str(item[1]) for item in resolved_items], lease_id

    async def lease_paths_async(
        self,
        artifact_ids: list[str],
    ) -> tuple[list[str], str | None]:
        """Lease paths with detached-result cleanup on caller cancellation."""

        def release_detached(result: tuple[list[str], str | None]) -> None:
            self.release_lease(result[1])

        return await _run_daemon_thread(
            lambda: self.lease_paths(artifact_ids),
            name="nanocat.artifact-lease-paths-io",
            on_detached=release_detached,
        )

    def public_refs(self, artifact_ids: list[str]) -> list[dict[str, Any]]:
        """Return stable public metadata for already-authorized artifact IDs."""
        with self._lock:
            refs: list[dict[str, Any]] = []
            for artifact_id in artifact_ids:
                resolved = self._resolve_locked(artifact_id)
                if resolved is None:
                    raise KeyError(artifact_id)
                refs.append(resolved[0].public())
            return refs

    async def public_refs_async(self, artifact_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve public references through a daemon I/O owner."""
        return await _run_daemon_thread(
            lambda: self.public_refs(artifact_ids),
            name="nanocat.artifact-public-refs-io",
        )

    def release_lease(self, lease_id: str | None) -> None:
        if lease_id is None:
            return
        with self._lease_lock:
            pinned = self._leases.pop(lease_id, ())
            for artifact_id in pinned:
                self._pins[artifact_id] -= 1
                if self._pins[artifact_id] <= 0:
                    self._pins.pop(artifact_id, None)

    def schedule_release_lease(self, lease_id: str | None) -> None:
        """Release a terminal-turn lease through isolated in-memory state."""
        if lease_id is None or self._closed:
            return
        self.release_lease(lease_id)

    async def close_reader(self, handle: BinaryIO, lease_id: str | None) -> None:
        """Close a response file and release its lease outside the event loop."""

        def close_owned() -> None:
            try:
                handle.close()
            finally:
                self.release_lease(lease_id)

        await _await_owned_bounded(
            _run_daemon_thread(
                close_owned,
                name=f"nanocat.artifact-reader-close-io.{lease_id or 'none'}",
            )
        )

    def open_for_read(self, artifact_id: str) -> tuple[ArtifactEntry, BinaryIO, str] | None:
        """Pin and open an artifact under one registry lock."""
        with self._lock:
            if self._closed:
                return None
            resolved = self._resolve_locked(artifact_id)
            if resolved is None:
                return None
            entry, path = resolved
            lease_id = uuid4().hex
            with self._lease_lock:
                self._leases[lease_id] = (artifact_id,)
                self._pins[artifact_id] += 1
            try:
                handle = path.open("rb")
            except OSError:
                self.release_lease(lease_id)
                return None
            return entry, handle, lease_id

    async def open_for_read_async(
        self,
        artifact_id: str,
    ) -> tuple[ArtifactEntry, BinaryIO, str] | None:
        """Open a reader with detached-result cleanup on caller cancellation."""

        def close_detached(opened: tuple[ArtifactEntry, BinaryIO, str] | None) -> None:
            if opened is None:
                return
            _, handle, lease_id = opened
            try:
                handle.close()
            finally:
                self.release_lease(lease_id)

        return await _run_daemon_thread(
            lambda: self.open_for_read(artifact_id),
            name=f"nanocat.artifact-open-reader-io.{artifact_id[:12]}",
            on_detached=close_detached,
        )

    def _close_state(self) -> bool:
        """Flush close state from a daemon owner with bounded lock acquisition."""
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_S)
        if not acquired:
            return False
        try:
            with self._lease_lock:
                self._leases.clear()
                self._pins.clear()
            if not self._degraded:
                self._save_locked()
        finally:
            self._lock.release()
        return True

    async def _close_owned(self) -> None:
        worker = asyncio.create_task(
            _run_daemon_thread(
                self._close_state,
                name="nanocat.artifact-close-io",
            ),
            name="nanocat.artifact-close",
        )
        try:
            completed = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=_CLOSE_LOCK_TIMEOUT_S * 2 + 0.1,
            )
        except TimeoutError:
            worker.cancel()
            completed = False
        if not completed:
            logger.error(
                "Artifact registry close reached its I/O deadline; "
                "startup recovery retains ownership"
            )

    async def close(self) -> None:
        owner = self._close_task
        if owner is None:
            self._closed = True
            owner = asyncio.create_task(
                self._close_owned(),
                name="nanocat.artifact-close-owner",
            )
            self._close_task = owner
        await asyncio.shield(owner)


def parse_range(value: str, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range; multiple ranges are intentionally unsupported."""
    if not value.startswith("bytes=") or "," in value or size <= 0:
        return None
    spec = value[6:].strip()
    start_text, separator, end_text = spec.partition("-")
    if not separator:
        return None
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return max(0, size - suffix), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def read_range(
    handle: BinaryIO,
    start: int,
    end: int,
    chunk_size: int = 1024 * 1024,
    on_close: Callable[[], None] | None = None,
):
    """Yield a bounded byte range from an already-authorized file."""
    handle.seek(start)
    remaining = end - start + 1
    try:
        while remaining:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        try:
            handle.close()
        finally:
            if on_close is not None:
                on_close()

"""QQ channel implementation using botpy SDK."""

import asyncio
import base64
import hashlib
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import httpx
from loguru import logger
from pydantic import Field

from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.base import BaseChannel
from nanocat.config.paths import get_media_dir
from nanocat.config.schema import Base
from nanocat.core.ports import ChannelCapabilities

try:
    import botpy
    from botpy.message import C2CMessage, GroupMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None
    GroupMessage = None

if TYPE_CHECKING:
    from botpy.message import C2CMessage, GroupMessage


def _bridge_botpy_logging() -> None:
    """Bridge botpy stdlib logs into Loguru (idempotent)."""
    import logging

    class _BotpyLoguruHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame, depth = frame.f_back, depth + 1
            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    botpy_logger = logging.getLogger("botpy")
    if not any(isinstance(h, _BotpyLoguruHandler) for h in botpy_logger.handlers):
        botpy_logger.handlers = [_BotpyLoguruHandler()]
        botpy_logger.propagate = False


_T = TypeVar("_T")


async def _with_retry(
    fn: Callable[[], Coroutine[Any, Any, _T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "QQ API",
) -> _T:
    """Run an async callable with exponential-backoff retry on any exception."""
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2**attempt)
            logger.warning(
                "{} attempt {}/{} failed: {}. Retrying in {:.0f}s…",
                label,
                attempt + 1,
                max_attempts,
                e,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


# Markers in a QQ API error meaning the passive-reply window expired (60 min C2C /
# 5 min group) or the per-message reply cap (5) / monthly active cap was hit — error
# code 22009 "msg limit exceed". Retrying these cannot succeed, so stop early.
_RATE_LIMIT_MARKERS = ("22009", "msg limit", "limit exceed", "push msg")


async def _post_message_with_retry(
    fn: Callable[[], Coroutine[Any, Any, Any]],
    *,
    label: str,
    max_attempts: int = 3,
    base_delay: float = 1.5,
) -> Any | None:
    """Send via a botpy ``post_*`` call, retrying on both exceptions and ``None`` results.

    botpy's HTTP layer swallows ``asyncio.TimeoutError`` and returns ``None`` WITHOUT
    raising (see ``botpy.http.BotHttp.request``), so exception-only retry never fires on a
    timeout and the message is silently dropped — the user sees no reply while the agent
    looks busy. Treat ``None`` as a failure and retry it; bail out early (no retry) on
    passive-window/rate-limit errors. Returns the API result dict, or ``None`` on failure.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            result = await fn()
        except Exception as e:
            if any(m in str(e).lower() for m in _RATE_LIMIT_MARKERS):
                logger.warning("{}: passive window expired / rate-limited, not retrying: {}", label, e)
                return None
            if attempt == max_attempts:
                logger.error("{}: failed after {} attempts: {}", label, max_attempts, e)
                return None
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("{}: attempt {}/{} error: {}; retry in {:.0f}s", label, attempt, max_attempts, e, delay)
            await asyncio.sleep(delay)
            continue
        if result is not None:
            return result
        # None = botpy swallowed a timeout and returned without raising
        if attempt == max_attempts:
            logger.error("{}: no API response after {} attempts (timeouts)", label, max_attempts)
            return None
        delay = base_delay * (2 ** (attempt - 1))
        logger.warning("{}: no response (timeout) attempt {}/{}; retry in {:.0f}s", label, attempt, max_attempts, delay)
        await asyncio.sleep(delay)
    return None


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log — NanoCat uses loguru; default "botpy.log" fails on read-only fs.
            # Raise the HTTP timeout: botpy defaults to 5 s, which fails often under instability.
            super().__init__(
                intents=intents, ext_handlers=False, timeout=channel.config.timeout
            )

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: "GroupMessage"):
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            await channel._on_message(message, is_group=False)

    return _Bot


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    msg_format: Literal["plain", "markdown"] = "plain"
    timeout: int = 20  # botpy HTTP timeout (s); default 5 is too short under instability


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"
    capabilities = ChannelCapabilities(media=True, reply_threads=True)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._msg_seq: int = 1
        self._chat_type_cache: dict[str, str] = {}
        self._reconnect_event = asyncio.Event()
        _bridge_botpy_logging()
    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        self._reconnect_event.clear()
        logger.info("QQ bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect and capped exponential backoff.

        The client is rebuilt each attempt so a stale session after an error does not
        wedge reconnection; backoff resets once a connection has stayed up for a while.
        """
        import time

        backoff = 5
        while self._running:
            self._client = _make_bot_class(self)()
            started = time.monotonic()
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            finally:
                try:
                    await self._client.close()
                except Exception:
                    pass
            if not self._running:
                break
            if time.monotonic() - started > 60:
                backoff = 5  # connection was healthy; reset backoff
            logger.info("Reconnecting QQ bot in {}s...", backoff)
            try:
                await asyncio.wait_for(self._reconnect_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            self._reconnect_event.clear()
            backoff = min(backoff * 2, 60)

    async def stop(self) -> None:
        """Stop the QQ bot."""
        self._running = False
        self._reconnect_event.set()
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        await self._cancel_owned_tasks()
        logger.info("QQ bot stopped")

    @staticmethod
    def _qq_file_type(path: str) -> int | None:
        """Map file extension to QQ file_type (1=image, 2=video, 3=audio, None=unsupported)."""
        ext = Path(path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return 1
        if ext == ".mp4":
            return 2
        if ext in (".silk", ".amr", ".wav", ".mp3", ".ogg", ".m4a"):
            return 3
        return 4

    _UPLOAD_TIMEOUT = 120  # seconds; base64 upload of large files needs more than the default 5s

    async def _upload_media(self, file_path: str, chat_type: str, chat_id: str) -> dict | None:
        """Upload a local file to QQ media API via base64.

        botpy SDK only exposes URL-based upload; the QQ API itself accepts file_data (base64),
        so we bypass the SDK wrapper and call the underlying HTTP session directly.
        BotHttp.timeout is temporarily raised for the upload because the default 5 s is far too
        short for large file payloads; async is cooperative so the swap is race-free.
        """
        from botpy.http import Route

        ft = self._qq_file_type(file_path)

        try:
            raw = Path(file_path).read_bytes()
            size_kb = len(raw) / 1024
            b64 = base64.b64encode(raw).decode()
            payload = {
                "file_type": ft,
                "file_data": b64,
                "file_name": Path(file_path).name,
                "srv_send_msg": False,
            }

            if chat_type == "group":
                route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=chat_id)
            else:
                route = Route("POST", "/v2/users/{openid}/files", openid=chat_id)

            logger.debug(
                "QQ: uploading {} ({:.1f} KB, file_type={})", Path(file_path).name, size_kb, ft
            )
            http = self._client.api._http
            saved_timeout = http.timeout
            http.timeout = self._UPLOAD_TIMEOUT
            try:
                result = await http.request(route, json=payload)
            finally:
                http.timeout = saved_timeout
            return result
        except Exception as e:
            logger.warning("QQ media upload failed ({}): {}", Path(file_path).name, e)
            return None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through QQ."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return

        try:
            msg_id = msg.metadata.get("message_id")
            use_markdown = self.config.msg_format == "markdown"
            chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")

            # Send media files before text
            for media_path in msg.media or []:
                try:
                    media = await self._upload_media(media_path, chat_type, msg.chat_id)
                    if not media:
                        continue
                    self._msg_seq += 1
                    m_payload: dict[str, Any] = {
                        "msg_type": 7,
                        "media": media,
                        "msg_id": msg_id,
                        "msg_seq": self._msg_seq,
                    }
                    if chat_type == "group":
                        await _post_message_with_retry(
                            lambda p=m_payload: self._client.api.post_group_message(
                                group_openid=msg.chat_id, **p
                            ),
                            label="QQ post_group_message (media)",
                        )
                    else:
                        await _post_message_with_retry(
                            lambda p=m_payload: self._client.api.post_c2c_message(
                                openid=msg.chat_id, **p
                            ),
                            label="QQ post_c2c_message (media)",
                        )
                except Exception as e:
                    logger.warning("QQ: failed to send media {}: {}", Path(media_path).name, e)

            if not (msg.content or "").strip():
                return

            self._msg_seq += 1
            payload: dict[str, Any] = {
                "msg_type": 2 if use_markdown else 0,
                "msg_id": msg_id,
                "msg_seq": self._msg_seq,
            }
            if use_markdown:
                payload["markdown"] = {"content": msg.content}
            else:
                payload["content"] = msg.content

            if chat_type == "group":
                result = await _post_message_with_retry(
                    lambda: self._client.api.post_group_message(
                        group_openid=msg.chat_id, **payload
                    ),
                    label="QQ post_group_message",
                )
            else:
                result = await _post_message_with_retry(
                    lambda: self._client.api.post_c2c_message(openid=msg.chat_id, **payload),
                    label="QQ post_c2c_message",
                )
            if result is None:
                logger.error(
                    "QQ message not delivered to {} (passive window may have expired "
                    "after slow processing, or the API is unreachable)",
                    msg.chat_id,
                )
        except Exception as e:
            logger.error("Error sending QQ message: {}", e)

    @staticmethod
    async def _convert_silk(silk_path: Path) -> Path | None:
        """Convert a QQ SILK v3 voice file to WAV via pilk.

        QQ voice messages use SILK v3 encoding regardless of the .amr extension.
        Returns the converted WAV path, or None on failure.
        """
        import importlib.util

        if importlib.util.find_spec("pilk") is None:
            logger.warning("QQ voice conversion requires 'pilk' (uv add pilk)")
            return None

        import pilk

        out_path = silk_path.with_suffix(".wav")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: pilk.silk_to_wav(str(silk_path), str(out_path), rate=24000)
            )
            if out_path.exists() and out_path.stat().st_size > 0:
                logger.debug(
                    "QQ SILK→WAV converted: {} ({} bytes)", out_path.name, out_path.stat().st_size
                )
                return out_path
            logger.warning("QQ SILK→WAV produced empty output: {}", silk_path.name)
        except Exception as e:
            logger.warning("QQ SILK→WAV conversion failed ({}): {}", silk_path.name, e)
        return None

    async def _download_attachment(
        self, attachment: Any, media_dir: Path
    ) -> tuple[str | None, str | None]:
        """Download a QQ message attachment. Returns (file_path, content_part)."""
        url = getattr(attachment, "url", None)
        content_type = getattr(attachment, "content_type", "") or ""
        filename = getattr(attachment, "filename", None) or "attachment"

        if not url:
            return None, None

        # Normalize protocol-relative URLs (//gchat.qpic.cn/...)
        if url.startswith("//"):
            url = "https:" + url

        if content_type.startswith("image/"):
            ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(content_type, ".jpg")
            media_type = "image"
        elif content_type in ("voice",) or content_type.startswith("audio/"):
            ext = ".amr"
            media_type = "voice"
        elif content_type.startswith("video/"):
            ext = ".mp4"
            media_type = "video"
        else:
            ext = Path(filename).suffix or ""
            media_type = "file"

        file_path = media_dir / f"{hashlib.md5(url.encode()).hexdigest()[:16]}{ext}"

        try:

            async def _download() -> None:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    file_path.write_bytes(resp.content)

            await _with_retry(_download, label=f"QQ attachment download ({media_type})")

            path_str = str(file_path)
            if media_type == "voice":
                transcription = await self.transcribe_audio(
                    await self._convert_silk(file_path) or file_path
                )
                if transcription:
                    logger.info("Transcribed QQ voice ({} chars)", len(transcription))
                    return path_str, f"[transcription: {transcription}]"
                return path_str, f"[voice: {path_str}]"
            return path_str, f"[{media_type}: {path_str}]"
        except Exception as e:
            logger.warning(
                "Failed to download QQ attachment ({})", type(e).__name__
            )
            return None, f"[{media_type}: download failed]"

    async def _on_message(self, data: "C2CMessage | GroupMessage", is_group: bool = False) -> None:
        """Handle incoming message from QQ."""
        try:
            # Dedup by message ID
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            if is_group:
                chat_id = data.group_openid
                user_id = data.author.member_openid
                self._chat_type_cache[chat_id] = "group"
            else:
                chat_id = str(
                    getattr(data.author, "id", None)
                    or getattr(data.author, "user_openid", "unknown")
                )
                user_id = chat_id
                self._chat_type_cache[chat_id] = "c2c"

            content_parts = []
            media_paths = []

            text = (data.content or "").strip()
            if text:
                content_parts.append(text)

            attachments = getattr(data, "attachments", None) or []
            if attachments:
                media_dir = get_media_dir("qq")
                for att in attachments:
                    path, part = await self._download_attachment(att, media_dir)
                    if path:
                        media_paths.append(path)
                    if part:
                        content_parts.append(part)

            if not content_parts and not media_paths:
                # QQ does not deliver forwarded/merged-forward (合并转发) content, stickers,
                # or some rich types to bots — they arrive with empty content and no
                # attachments. Surface a placeholder so the agent can reply instead of the
                # user getting silence (which looks like the bot ignored them).
                logger.info("QQ: unparseable message from {} (forwarded/sticker/unsupported)", user_id)
                content_parts.append(
                    "[Received a message with no readable text or media — likely a "
                    "forwarded/merged message, sticker, or a type QQ does not deliver to "
                    "bots. Tell the user you cannot read it and ask them to paste the "
                    "content as text.]"
                )

            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content="\n".join(content_parts) if content_parts else "[empty message]",
                media=media_paths,
                metadata={"message_id": data.id},
            )
        except Exception:
            logger.exception("Error handling QQ message")

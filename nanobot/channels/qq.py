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

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base

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


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log — nanobot uses loguru; default "botpy.log" fails on read-only fs
            super().__init__(intents=intents, ext_handlers=False)

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


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"

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
        self._msg_seq: int = 1  # 消息序列号，避免被 QQ API 去重
        self._chat_type_cache: dict[str, str] = {}

    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        bot_class = _make_bot_class(self)
        self._client = bot_class()
        logger.info("QQ bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the QQ bot."""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
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
                        await _with_retry(
                            lambda p=m_payload: self._client.api.post_group_message(
                                group_openid=msg.chat_id, **p
                            ),
                            label="QQ post_group_message (media)",
                        )
                    else:
                        await _with_retry(
                            lambda p=m_payload: self._client.api.post_c2c_message(
                                openid=msg.chat_id, **p
                            ),
                            label="QQ post_c2c_message (media)",
                        )
                except Exception as e:
                    logger.warning("QQ: failed to send media {}: {}", Path(media_path).name, e)

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
                await _with_retry(
                    lambda: self._client.api.post_group_message(
                        group_openid=msg.chat_id, **payload
                    ),
                    label="QQ post_group_message",
                )
            else:
                await _with_retry(
                    lambda: self._client.api.post_c2c_message(openid=msg.chat_id, **payload),
                    label="QQ post_c2c_message",
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
                    logger.info("Transcribed QQ voice: {}...", transcription[:50])
                    return path_str, f"[transcription: {transcription}]"
                return path_str, f"[voice: {path_str}]"
            return path_str, f"[{media_type}: {path_str}]"
        except Exception as e:
            logger.warning("Failed to download QQ attachment {}: {}", url, e)
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
                return

            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content="\n".join(content_parts) if content_parts else "[empty message]",
                media=media_paths,
                metadata={"message_id": data.id},
            )
        except Exception:
            logger.exception("Error handling QQ message")

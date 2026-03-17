"""QQ channel implementation using botpy SDK."""

import asyncio
import hashlib
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through QQ."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return

        try:
            msg_id = msg.metadata.get("message_id")
            self._msg_seq += 1
            use_markdown = self.config.msg_format == "markdown"
            payload: dict[str, Any] = {
                "msg_type": 2 if use_markdown else 0,
                "msg_id": msg_id,
                "msg_seq": self._msg_seq,
            }
            if use_markdown:
                payload["markdown"] = {"content": msg.content}
            else:
                payload["content"] = msg.content

            chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")
            if chat_type == "group":
                await self._client.api.post_group_message(
                    group_openid=msg.chat_id,
                    **payload,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=msg.chat_id,
                    **payload,
                )
        except Exception as e:
            logger.error("Error sending QQ message: {}", e)

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
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                file_path.write_bytes(resp.content)

            path_str = str(file_path)
            if media_type == "voice":
                transcription = await self.transcribe_audio(file_path)
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

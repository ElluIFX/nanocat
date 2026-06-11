"""Voice transcription provider using the OpenAI-compatible Whisper API."""

from pathlib import Path

import httpx
from loguru import logger


class WhisperTranscriptionProvider:
    """Voice transcription via any OpenAI-compatible Whisper endpoint."""

    def __init__(self, api_key: str, api_url: str, model: str = "whisper-large-v3"):
        self.api_key = api_key
        self.api_url = api_url
        self.model = model

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text, or empty string on failure.
        """
        if not self.api_key:
            logger.warning("Whisper API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    response = await client.post(
                        self.api_url,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        data={"model": self.model},
                        files={"file": (path.name, f)},
                        timeout=60.0,
                    )

                    if not response.is_success:
                        logger.error(
                            "Whisper transcription error: {} {}\n{}",
                            response.status_code,
                            response.reason_phrase,
                            response.text,
                        )
                        return ""

                    return response.json().get("text", "")

        except Exception as e:
            logger.error("Whisper transcription error: {}", e)
            return ""

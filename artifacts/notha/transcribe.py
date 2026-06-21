"""
Audio transcription via OpenAI Whisper.

Receives raw audio bytes (typically OGG/Opus from WhatsApp) and returns
the transcribed text. Uses the same OpenAI provider already configured in the project.
"""
import io
import logging
import os

logger = logging.getLogger("notha.transcribe")

# Default extension mapping for WhatsApp audio formats
_MIME_TO_EXT = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".mp4",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/amr": ".amr",
}


def _ext_from_mime(mime_type: str) -> str:
    """Returns the correct file extension for the given MIME type."""
    base = mime_type.split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(base, ".ogg")


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """Transcribes audio bytes using OpenAI Whisper.

    Returns the transcribed text or None on failure.
    Supported formats: OGG (Opus), MP3, MP4, AAC, WAV, WebM, AMR.
    """
    if not audio_bytes:
        logger.warning("transcribe_audio: empty audio bytes")
        return None

    try:
        from openai import AsyncOpenAI

        replit_base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
        replit_api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
        direct_api_key = os.environ.get("OPENAI_API_KEY")

        if replit_base_url and replit_api_key:
            client = AsyncOpenAI(base_url=replit_base_url, api_key=replit_api_key)
        elif direct_api_key:
            client = AsyncOpenAI(api_key=direct_api_key)
        else:
            logger.error("transcribe_audio: OpenAI not configured")
            return None

        ext = _ext_from_mime(mime_type)
        filename = f"audio{ext}"

        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = filename

        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_bytes, mime_type.split(";")[0].strip()),
        )

        text = (response.text or "").strip()
        if text:
            logger.info("Audio transcribed successfully (%d bytes → %d chars)", len(audio_bytes), len(text))
        else:
            logger.warning("Whisper returned empty transcription")
        return text or None

    except Exception as e:
        logger.error("Error transcribing audio: %s", e)
        return None

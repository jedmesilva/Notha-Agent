"""
Transcrição de áudio via OpenAI Whisper.

Recebe os bytes brutos do arquivo de áudio (geralmente OGG/Opus vindo do WhatsApp)
e devolve o texto transcrito. Usa o mesmo provider OpenAI já configurado no projeto.
"""
import io
import logging
import os

logger = logging.getLogger("notha.transcribe")

# Extensão padrão para áudios do WhatsApp (OGG Opus)
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
    """Retorna a extensão de arquivo correta para o MIME type informado."""
    base = mime_type.split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(base, ".ogg")


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """Transcreve bytes de áudio usando OpenAI Whisper.

    Retorna o texto transcrito ou None em caso de falha.
    Suporta os formatos: OGG (Opus), MP3, MP4, AAC, WAV, WebM, AMR.
    """
    if not audio_bytes:
        logger.warning("transcribe_audio: bytes de áudio vazios")
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
            logger.error("transcribe_audio: OpenAI não configurado")
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
            logger.info("Áudio transcrito com sucesso (%d bytes → %d chars)", len(audio_bytes), len(text))
        else:
            logger.warning("Whisper retornou transcrição vazia")
        return text or None

    except Exception as e:
        logger.error("Erro ao transcrever áudio: %s", e)
        return None

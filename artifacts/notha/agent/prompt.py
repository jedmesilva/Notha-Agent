import os
import logging

logger = logging.getLogger("notha.prompt")

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "..", "system_prompt.txt")


def load_system_prompt() -> str:
    """
    Carrega o prompt do arquivo system_prompt.txt.
    Pode ser sobrescrito via variável de ambiente SYSTEM_PROMPT.
    """
    env_prompt = os.environ.get("SYSTEM_PROMPT")
    if env_prompt:
        logger.info("Usando SYSTEM_PROMPT da variável de ambiente.")
        return env_prompt

    try:
        with open(_PROMPT_FILE, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
        logger.info("system_prompt.txt carregado com sucesso.")
        return prompt
    except FileNotFoundError:
        logger.warning("system_prompt.txt não encontrado. Usando prompt padrão.")
        return "Você é o Notha, um assistente prestativo disponível pelo WhatsApp."

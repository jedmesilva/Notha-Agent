import os

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

WHATSAPP_ACCESS_TOKEN: str = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID: str = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN: str = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

ASAAS_API_KEY: str = os.environ.get("ASAAS_API_KEY", "")
ASAAS_BASE_URL: str = os.environ.get("ASAAS_BASE_URL", "https://sandbox.asaas.com/api/v3")

LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai")
OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL: str = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

MAX_RODADAS_PROXY: int = int(os.environ.get("MAX_RODADAS_PROXY", "6"))
MAX_TENTATIVAS_HUMANAS: int = int(os.environ.get("MAX_TENTATIVAS_HUMANAS", "3"))
AJUSTE_MAXIMO_PERMITIDO: float = float(os.environ.get("AJUSTE_MAXIMO_PERMITIDO", "-0.15"))
TIMEOUT_RODADA_MINUTOS: int = int(os.environ.get("TIMEOUT_RODADA_MINUTOS", "30"))
EXPIRACAO_TOTAL_HORAS: int = int(os.environ.get("EXPIRACAO_TOTAL_HORAS", "24"))
PRAZO_ESTORNO_POS_FALHA_DIAS: int = int(os.environ.get("PRAZO_ESTORNO_POS_FALHA_DIAS", "3"))

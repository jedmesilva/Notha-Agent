import os

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY: str = os.environ.get("SUPABASE_ANON_KEY", "")

WHATSAPP_ACCESS_TOKEN: str = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID: str = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN: str = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

ASAAS_API_KEY: str = os.environ.get("ASAAS_API_KEY", "")
ASAAS_BASE_URL: str = os.environ.get("ASAAS_BASE_URL", "https://sandbox.asaas.com/api/v3")

LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai")
OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL: str = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

# Env var names kept in Portuguese to match already-deployed Railway/Replit secrets
MAX_PROXY_ROUNDS: int = int(os.environ.get("MAX_RODADAS_PROXY", "6"))
MAX_HUMAN_ATTEMPTS: int = int(os.environ.get("MAX_TENTATIVAS_HUMANAS", "3"))
MAX_ALLOWED_ADJUSTMENT: float = float(os.environ.get("AJUSTE_MAXIMO_PERMITIDO", "-0.15"))
ROUND_TIMEOUT_MINUTES: int = int(os.environ.get("TIMEOUT_RODADA_MINUTOS", "30"))
TOTAL_EXPIRATION_HOURS: int = int(os.environ.get("EXPIRACAO_TOTAL_HORAS", "24"))
REFUND_DEADLINE_AFTER_FAILURE_DAYS: int = int(os.environ.get("PRAZO_ESTORNO_POS_FALHA_DIAS", "3"))

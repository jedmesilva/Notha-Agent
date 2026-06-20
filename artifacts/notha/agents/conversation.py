"""
Conversation Agent — única interface de linguagem natural com humanos.

Responsabilidades:
  - Interpretar mensagens em linguagem natural e extrair intenção estruturada (JSON)
  - Traduzir decisões estruturadas do sistema em respostas naturais
  - Manter tom humano e contextual durante toda a interação

NÃO decide preços, NÃO acessa Asaas, NÃO mantém memória própria.
"""
import json
import logging
from openai import AsyncOpenAI
from config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger("notha.agent.conversation")

SYSTEM_PROMPT_TEMPLATE = """Você é o NOTHA, um agente de inteligência artificial que intermedia a compra e venda de produtos físicos via WhatsApp.

Papel atual: {role}

Produto em negociação: {produto_info}
Status da negociação: {status_negociacao}

Diretrizes:
- Seja direto, humano e cordial. Nunca reverifique informações que já foram confirmadas.
- Nunca revele o preço mínimo do vendedor ao comprador, nem o limite máximo do comprador ao vendedor.
- Quando precisar coletar informações (CPF, endereço, chave Pix), explique brevemente o motivo.
- Extraia intenções estruturadas das mensagens quando solicitado.
- Responda sempre em português brasileiro informal mas educado.
- Nunca prometa valores ou condições que o sistema não confirmou.
"""

INTENT_EXTRACTION_PROMPT = """Analise a mensagem abaixo e extraia a intenção estruturada em JSON.

Mensagem: "{mensagem}"
Contexto: {contexto}

Retorne SOMENTE um JSON válido com os campos relevantes para o contexto. Exemplos de campos:
- Para oferta/contraproposta: {{"intencao": "contraproposta", "valor_estimado": 200, "confianca": "media", "contexto_extra": null}}
- Para confirmação: {{"intencao": "confirmacao", "aceitou": true}}
- Para recusa: {{"intencao": "recusa", "aceitou": false, "motivo": "caro demais"}}
- Para cadastro de produto: {{"intencao": "listar_produto", "descricao": "...", "categoria": "...", "preco_informado": null}}
- Para busca de produto: {{"intencao": "buscar_produto", "categoria": "...", "descricao_busca": "..."}}
- Para informação pessoal: {{"intencao": "informar_dados", "campo": "cpf|nome|endereco|chave_pix", "valor": "..."}}
- Para comando geral: {{"intencao": "outro", "descricao": "..."}}
"""


def _make_client() -> AsyncOpenAI:
    if OPENAI_API_KEY:
        return AsyncOpenAI(api_key=OPENAI_API_KEY)
    api_key = os.environ.get("OPENAI_API_KEY", "nokey")
    return AsyncOpenAI(api_key=api_key, base_url=OPENAI_BASE_URL)


class ConversationAgent:
    def __init__(self):
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = _make_client()
        return self._client

    async def respond(
        self,
        phone: str,
        user_message: str,
        history: list[dict],
        role: str = "geral",
        produto_info: str = "nenhum produto em contexto",
        status_negociacao: str = "sem negociação ativa",
    ) -> str:
        system = SYSTEM_PROMPT_TEMPLATE.format(
            role=role,
            produto_info=produto_info,
            status_negociacao=status_negociacao,
        )
        messages = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.7,
                max_tokens=500,
            )
            return resp.choices[0].message.content or "Desculpe, não consegui responder agora."
        except Exception as e:
            logger.error(f"Erro no ConversationAgent.respond: {e}")
            return "Desculpe, tive um problema técnico. Tente novamente em instantes."

    async def extract_intent(self, mensagem: str, contexto: str = "geral") -> dict:
        prompt = INTENT_EXTRACTION_PROMPT.format(mensagem=mensagem, contexto=contexto)
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            return json.loads(raw)
        except Exception as e:
            logger.error(f"Erro ao extrair intenção: {e}")
            return {"intencao": "outro", "descricao": mensagem}

    async def build_reply(self, instrucao: str, contexto: dict | None = None) -> str:
        """Transforma uma instrução estruturada do sistema em linguagem natural."""
        ctx_str = json.dumps(contexto or {}, ensure_ascii=False)
        messages = [
            {"role": "system", "content": "Você é o NOTHA. Transforme a instrução abaixo em uma mensagem natural para o usuário do WhatsApp. Seja breve e cordial. Responda em português."},
            {"role": "user", "content": f"Instrução: {instrucao}\nContexto: {ctx_str}"},
        ]
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.6,
                max_tokens=300,
            )
            return resp.choices[0].message.content or instrucao
        except Exception as e:
            logger.error(f"Erro ao construir reply: {e}")
            return instrucao

    async def ask_confirmation(self, instrucao: str, contexto: dict | None = None) -> str:
        """Gera uma pergunta de confirmação clara para o usuário."""
        ctx_str = json.dumps(contexto or {}, ensure_ascii=False)
        messages = [
            {"role": "system", "content": "Você é o NOTHA. Crie uma pergunta de confirmação direta (sim/não) para o usuário, com base na instrução. Use português informal mas claro. Apresente o valor em R$ se relevante."},
            {"role": "user", "content": f"Instrução: {instrucao}\nContexto: {ctx_str}"},
        ]
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.5,
                max_tokens=200,
            )
            return resp.choices[0].message.content or instrucao
        except Exception as e:
            logger.error(f"Erro ao construir confirmação: {e}")
            return instrucao

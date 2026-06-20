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
import os
from openai import AsyncOpenAI
from config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger("notha.agent.conversation")

SYSTEM_PROMPT_TEMPLATE = """Você é o NOTHA — um agente de negociação de produtos físicos que opera 100% pelo WhatsApp.

Sua função é intermediar compras e vendas de produtos usados ou novos entre pessoas reais, de forma segura, rápida e justa. Você gerencia a negociação, o pagamento via Pix e a entrega, tudo pelo chat.

━━━ IDENTIDADE E TOM ━━━
- Nome: NOTHA
- Tom: direto, humano, informal mas profissional — como um amigo de confiança que entende de negócios
- Linguagem: português brasileiro coloquial, sem gírias excessivas, sem formalidade corporativa
- Evite frases genéricas como "Claro!", "Com certeza!", "Posso te ajudar?"
- Seja objetivo: vá direto ao ponto em no máximo 3 frases curtas quando possível
- Use emojis com moderação (1-2 por mensagem no máximo) quando natural para o contexto
- Nunca use markdown (asteriscos, hashtags) — o WhatsApp formata de forma diferente

━━━ CONTEXTO ATUAL ━━━
Nome do usuário: {usuario_nome}
Papel do usuário nesta conversa: {role}
Produto em contexto: {produto_info}
Status da negociação: {status_negociacao}

━━━ REGRAS INEGOCIÁVEIS ━━━
1. NUNCA revele o preço mínimo do vendedor ao comprador
2. NUNCA revele o limite máximo do comprador ao vendedor
3. NUNCA prometa um valor, prazo ou condição que o sistema não confirmou
4. NUNCA peça informação que já foi confirmada anteriormente nessa conversa
5. NUNCA mencione "inteligência artificial", "LLM", "GPT" ou "algoritmo" — você é o NOTHA
6. Se o usuário perguntar se você é robô: confirme que é um sistema automatizado, mas não entre em detalhes técnicos
7. Em caso de conflito ou reclamação grave: oriente o usuário a responder "SUPORTE" para falar com um humano

━━━ SOBRE PAGAMENTOS ━━━
- Pagamentos são feitos via Pix (QR Code ou chave Pix)
- O valor fica retido com segurança até a confirmação de entrega por ambas as partes
- Apenas após confirmação mútua o valor é liberado para o vendedor
- Taxa do NOTHA já está incluída — não detalhe o valor da taxa para o usuário

━━━ QUANDO COLETAR DADOS PESSOAIS ━━━
Ao pedir CPF: "Preciso do seu CPF só para emitir o comprovante da transação — é seguro e não compartilhamos com terceiros."
Ao pedir chave Pix: "Qual sua chave Pix para receber o pagamento? Pode ser CPF, e-mail, celular ou chave aleatória."
Ao pedir endereço: "Me passa o endereço completo para entrega (rua, número, bairro, cidade e CEP)."

━━━ PAPEL ATUAL ━━━
- vendedor: o usuário está cadastrando um produto para vender. Seja encorajador, ajude a descrever bem o produto.
- comprador: o usuário quer comprar algo. Ajude-o a encontrar o que busca e entender o processo.
- entregador: o usuário vai fazer uma entrega. Seja prático e claro sobre rota, valor e confirmação.
- geral: primeiro contato ou situação não mapeada. Identifique o que o usuário quer e oriente.
"""

INTENT_EXTRACTION_PROMPT = """Você é um extrator de intenção para o sistema NOTHA de negociação de produtos via WhatsApp.

Analise a mensagem abaixo e extraia a intenção estruturada em JSON.

Mensagem do usuário: "{mensagem}"
Contexto atual: {contexto}

━━━ INSTRUÇÕES ━━━
- Retorne SOMENTE JSON válido, sem texto extra
- Se houver um valor monetário escrito por extenso (ex: "duzentos reais", "mil e quinhentos"), converta para número
- Se o usuário confirmar com "sim", "pode ser", "tá bom", "fechou", "aceito", "combinado", "ok" → intencao: "confirmacao", aceitou: true
- Se o usuário recusar com "não", "caro demais", "não quero", "desisto", "cancelar" → intencao: "recusa", aceitou: false
- Se houver um valor mencionado em contexto de oferta ou contraproposta, extraia o número

━━━ EXEMPLOS POR INTENCAO ━━━

Oferta / contraproposta de preço:
{{"intencao": "contraproposta", "valor_estimado": 350.0, "confianca": "alta", "contexto_extra": null}}

Confirmação simples (sim/aceito/fechou):
{{"intencao": "confirmacao", "aceitou": true}}

Recusa simples (não/caro/desisto):
{{"intencao": "recusa", "aceitou": false, "motivo": "achou caro"}}

Cadastro de produto para vender:
{{"intencao": "listar_produto", "descricao": "iPhone 13 128GB preto, sem arranhões", "categoria": "eletrônicos", "preco_informado": 2200.0}}

Busca de produto para comprar:
{{"intencao": "buscar_produto", "categoria": "eletrônicos", "descricao_busca": "notebook usado até R$1500"}}

Informação pessoal fornecida pelo usuário:
{{"intencao": "informar_dados", "campo": "cpf", "valor": "123.456.789-00"}}
{{"intencao": "informar_dados", "campo": "chave_pix", "valor": "meuemail@gmail.com"}}
{{"intencao": "informar_dados", "campo": "endereco", "valor": "Rua das Flores, 123, Centro, São Paulo, SP, 01001-000"}}
{{"intencao": "informar_dados", "campo": "nome", "valor": "João Silva"}}

Solicitação de suporte humano:
{{"intencao": "suporte", "motivo": "reclamação sobre entrega"}}

Confirmação de entrega pelo comprador:
{{"intencao": "confirmar_entrega", "recebeu": true}}

Confirmação de entrega pelo vendedor:
{{"intencao": "confirmar_entrega_vendedor", "entregou": true}}

Comando não mapeado / conversa geral:
{{"intencao": "outro", "descricao": "usuário perguntou sobre horário de funcionamento"}}
"""

REPLY_SYSTEM_PROMPT = """Você é o NOTHA, agente de negociação de produtos físicos via WhatsApp.

Transforme a instrução abaixo em uma mensagem natural, curta e direta para enviar pelo WhatsApp.

Regras:
- No máximo 3 frases curtas
- Tom informal mas profissional (como uma pessoa real de confiança)
- Português brasileiro coloquial — sem "Prezado", sem "Venho por meio desta"
- Sem markdown (sem *, sem #, sem _)
- Emojis: 0 a 2, somente se ficarem naturais
- Nunca mencione "sistema", "algoritmo", "banco de dados" ou termos técnicos
- Se o contexto tiver um valor em R$, sempre formate como "R$ 1.200,00" (vírgula para centavos, ponto para milhar)
"""

CONFIRMATION_SYSTEM_PROMPT = """Você é o NOTHA, agente de negociação de produtos físicos via WhatsApp.

Crie uma pergunta de confirmação clara e direta (resposta esperada: sim ou não).

Regras:
- Uma única pergunta, máximo 2 frases
- Tom direto e amigável
- Português coloquial
- Sem markdown
- Se houver valor em R$, formate como "R$ 1.200,00"
- Termine com "Confirma?" ou "Pode confirmar?" ou variação natural
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
        usuario_nome: str = "não informado ainda",
    ) -> str:
        system = SYSTEM_PROMPT_TEMPLATE.format(
            role=role,
            produto_info=produto_info,
            status_negociacao=status_negociacao,
            usuario_nome=usuario_nome,
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
            return resp.choices[0].message.content or "Desculpe, não consegui responder agora. Tente de novo."
        except Exception as e:
            logger.error(f"Erro no ConversationAgent.respond: {e}")
            return "Tive um problema técnico agora. Me manda de novo em instantes!"

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
            {"role": "system", "content": REPLY_SYSTEM_PROMPT},
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
            {"role": "system", "content": CONFIRMATION_SYSTEM_PROMPT},
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

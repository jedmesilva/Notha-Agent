"""
Guardrail de saída — valida a resposta do agente antes de enviar ao usuário.

Fluxo:
  1. Geração normal da resposta pelo agente
  2. Guardrail analisa a resposta com contexto + histórico
  3. Se aprovada → envia normalmente
  4. Se reprovada → tenta corrigir (uma vez) com instrução específica
  5. Se ainda reprovada → retorna fallback seguro genérico

Verifica:
  - Coerência com a mensagem do usuário e o histórico recente
  - Vazamento de dados sensíveis (preço mínimo do vendedor, limite do comprador)
  - Termos proibidos (GPT, IA, algoritmo, OpenAI, Anthropic, Claude etc.)
  - Valores monetários inventados sem chamada de ferramenta
  - Quebra de outras regras inegociáveis do NOTHA
"""
import json
import logging

logger = logging.getLogger("notha.guardrail")

_FALLBACK_SEGURO = (
    "Desculpe, tive um problema ao formular minha resposta. "
    "Pode repetir o que você precisa?"
)

_GUARDRAIL_PROMPT = """Você é um auditor de qualidade do NOTHA, sistema de compra e venda pelo WhatsApp.

Analise a RESPOSTA GERADA e verifique se ela é adequada para enviar ao usuário.

━━━ CONTEXTO DO USUÁRIO ━━━
{contexto}

━━━ HISTÓRICO RECENTE (últimas mensagens) ━━━
{historico}

━━━ ÚLTIMA MENSAGEM DO USUÁRIO ━━━
{ultima_mensagem}

━━━ RESPOSTA GERADA PELO AGENTE ━━━
{resposta}

━━━ CRITÉRIOS DE AVALIAÇÃO ━━━
Reprove (aprovado: false) se qualquer condição abaixo for verdadeira:

1. INCOERÊNCIA: A resposta não tem relação com a última mensagem do usuário, ou ignora completamente o assunto tratado na conversa.
2. VAZAMENTO DE DADOS: A resposta revela ao comprador o preço mínimo do vendedor, ou revela ao vendedor o limite máximo do comprador.
3. TERMOS PROIBIDOS: A resposta menciona "GPT", "OpenAI", "Anthropic", "Claude", "LLM", "inteligência artificial", "algoritmo" ou qualquer referência ao modelo de IA subjacente.
4. VALOR INVENTADO: A resposta cita um valor monetário específico (ex: "R$ 350", "USD 100") que não estava no histórico da conversa nem foi confirmado pelo sistema.
5. PROMESSA NÃO CONFIRMADA: A resposta promete prazo, condição ou funcionalidade que o sistema não confirmou (ex: "vou te avisar amanhã", "você receberá em 2 dias").
6. SEM SENTIDO: A resposta está truncada, incompleta, é só caracteres aleatórios, ou não faz sentido algum em português ou no idioma do usuário.
7. INFORMAÇÃO REPETIDA DESNECESSARIAMENTE: A resposta pede ao usuário uma informação que ele já forneceu claramente nessa mesma conversa (ex: perguntar o nome de alguém que já se identificou).

Aprove (aprovado: true) se:
- A resposta é coerente com a conversa e a mensagem do usuário
- Não viola nenhum dos critérios acima
- Está em linguagem natural adequada para WhatsApp

━━━ FORMATO DE RETORNO ━━━
Retorne SOMENTE JSON válido, sem explicações extras:

Se aprovada:
{{"aprovado": true}}

Se reprovada:
{{"aprovado": false, "categoria": "<categoria>", "motivo": "<motivo conciso>"}}

Categorias possíveis: incoerencia | vazamento_dados | termo_proibido | valor_inventado | promessa_nao_confirmada | sem_sentido | repeticao_desnecessaria
"""

_CORRECAO_PROMPT = """A resposta que você gerou foi reprovada pelo sistema de qualidade.

Motivo da reprovação: {motivo} (categoria: {categoria})

━━━ CONTEXTO DO USUÁRIO ━━━
{contexto}

━━━ ÚLTIMA MENSAGEM DO USUÁRIO ━━━
{ultima_mensagem}

━━━ RESPOSTA REPROVADA ━━━
{resposta_reprovada}

Gere uma nova resposta corrigindo especificamente o problema apontado.
Mantenha o tom adequado para WhatsApp. Seja conciso. Não mencione que houve reprovação.
Retorne SOMENTE a nova resposta, sem explicações."""


async def _chamar_llm_guardrail(messages: list[dict]) -> dict:
    """Chama o LLM no modo JSON para avaliar a resposta."""
    from llm import get_provider
    try:
        resp = await get_provider().complete(
            messages=messages,
            temperature=0.0,
            max_tokens=200,
            json_mode=True,
        )
        return json.loads(resp.text or '{"aprovado": true}')
    except Exception as e:
        logger.error("Erro no guardrail (LLM): %s", e)
        return {"aprovado": True}


async def _chamar_llm_correcao(messages: list[dict]) -> str:
    """Pede ao LLM que corrija a resposta reprovada."""
    from llm import get_provider
    try:
        resp = await get_provider().complete(
            messages=messages,
            temperature=0.4,
            max_tokens=400,
        )
        return resp.text or _FALLBACK_SEGURO
    except Exception as e:
        logger.error("Erro na correção do guardrail (LLM): %s", e)
        return _FALLBACK_SEGURO


def _formatar_historico(history: list[dict], max_mensagens: int = 10) -> str:
    """Formata as últimas mensagens do histórico de forma legível."""
    recentes = [m for m in history if m.get("role") in ("user", "assistant")][-max_mensagens:]
    linhas = []
    for m in recentes:
        role = "Usuário" if m["role"] == "user" else "NOTHA"
        conteudo = m.get("content") or ""
        if conteudo:
            linhas.append(f"{role}: {conteudo[:300]}")
    return "\n".join(linhas) if linhas else "(sem histórico anterior)"


def _ultima_mensagem_usuario(history: list[dict]) -> str:
    """Extrai a última mensagem do usuário do histórico."""
    for m in reversed(history):
        if m.get("role") == "user" and m.get("content"):
            return m["content"]
    return ""


async def validar_resposta(
    resposta: str,
    history: list[dict],
    contexto: str,
    user_message: str = "",
) -> str:
    """
    Valida a resposta gerada antes de enviar ao usuário.

    Parâmetros:
      resposta     — texto gerado pelo agente
      history      — histórico completo da conversa (roles: user/assistant)
      contexto     — string de contexto do usuário (do _build_context)
      user_message — última mensagem do usuário (se não estiver no history)

    Retorna a resposta original se aprovada, uma versão corrigida se possível,
    ou um fallback seguro se o problema persistir.
    """
    if not resposta or not resposta.strip():
        logger.warning("Guardrail: resposta vazia recebida — usando fallback.")
        return _FALLBACK_SEGURO

    ultima = user_message or _ultima_mensagem_usuario(history)
    historico_fmt = _formatar_historico(history)

    # ── Fase 1: Avaliação ────────────────────────────────────────────────────
    prompt_avaliacao = _GUARDRAIL_PROMPT.format(
        contexto=contexto or "sem contexto",
        historico=historico_fmt,
        ultima_mensagem=ultima or "(sem mensagem)",
        resposta=resposta,
    )
    resultado = await _chamar_llm_guardrail([
        {"role": "user", "content": prompt_avaliacao}
    ])

    if resultado.get("aprovado", True):
        return resposta

    categoria = resultado.get("categoria", "desconhecido")
    motivo = resultado.get("motivo", "sem motivo especificado")
    logger.warning("Guardrail REPROVADO — categoria=%s | motivo=%s", categoria, motivo)

    # ── Fase 2: Tentativa de correção ────────────────────────────────────────
    prompt_correcao = _CORRECAO_PROMPT.format(
        motivo=motivo,
        categoria=categoria,
        contexto=contexto or "sem contexto",
        ultima_mensagem=ultima or "(sem mensagem)",
        resposta_reprovada=resposta,
    )
    resposta_corrigida = await _chamar_llm_correcao([
        {"role": "user", "content": prompt_correcao}
    ])

    # ── Fase 3: Re-avaliação da resposta corrigida ───────────────────────────
    prompt_re_avaliacao = _GUARDRAIL_PROMPT.format(
        contexto=contexto or "sem contexto",
        historico=historico_fmt,
        ultima_mensagem=ultima or "(sem mensagem)",
        resposta=resposta_corrigida,
    )
    resultado_final = await _chamar_llm_guardrail([
        {"role": "user", "content": prompt_re_avaliacao}
    ])

    if resultado_final.get("aprovado", True):
        logger.info("Guardrail: resposta corrigida aprovada na segunda tentativa.")
        return resposta_corrigida

    logger.error(
        "Guardrail: resposta corrigida também reprovada — usando fallback seguro. "
        "categoria=%s | motivo=%s",
        resultado_final.get("categoria"),
        resultado_final.get("motivo"),
    )
    return _FALLBACK_SEGURO

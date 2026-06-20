"""
ListingFlowAgent — máquina de estados para cadastro completo de produto via WhatsApp.

Etapas:
  produto       → O que você quer vender?
  marca_modelo  → Marca, modelo e versão
  estado_uso    → Novo ou usado?
  condicao      → Estado de conservação
  nota_fiscal   → Tem nota fiscal?
  fotos         → Fotos do produto (múltiplas; texto = "pronto")
  endereco      → Endereço de retirada
  preco         → Preço desejado e mínimo aceitável
  processando   → [automático] busca web + banco + visão + precificação
  confirmar     → Resumo e confirmação
  concluido     → Listing criado
"""
import json
import logging
import os
from openai import AsyncOpenAI
from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL

logger = logging.getLogger("notha.agent.listing_flow")


def _parse_jsonb(value, default):
    """Converte valor JSONB do asyncpg para Python — suporta dict/list ou string JSON."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


CONDICAO_LABEL = {
    "como_novo": "Como novo (sem marcas de uso)",
    "bom": "Bom estado (uso leve, poucas marcas)",
    "conservado": "Conservado (uso normal, pequenos desgastes)",
    "desgastado": "Desgastado (uso intenso, marcas visíveis)",
    "com_defeito": "Com defeito (funciona parcialmente ou não funciona)",
}


def _make_client() -> AsyncOpenAI:
    if OPENAI_API_KEY:
        return AsyncOpenAI(api_key=OPENAI_API_KEY)
    api_key = os.environ.get("OPENAI_API_KEY", "nokey")
    return AsyncOpenAI(api_key=api_key, base_url=OPENAI_BASE_URL)


class ListingFlowAgent:
    def __init__(self):
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = _make_client()
        return self._client

    # ─────────────────────────────────────────────
    # Guardrails — extração com evidência e retry
    # ─────────────────────────────────────────────

    _EXTRACT_GUARDRAIL = (
        "\n\nRETORNE SEMPRE UM JSON VÁLIDO seguindo estas REGRAS DE EXTRAÇÃO (OBRIGATÓRIAS):\n"
        "1. Para cada campo extraído, inclua um campo 'evidencia_<campo>' com o trecho "
        "   EXATO da mensagem do usuário que embasa o valor. Se não houver trecho que sustente, "
        "   o campo principal DEVE ser null e 'evidencia_<campo>' DEVE ser null.\n"
        "2. NUNCA invente, infira ou suponha valores. Só extraia o que foi dito explicitamente.\n"
        "3. NUNCA complete informações implícitas (ex: o usuário disse 'iPhone 13' sem citar 'Apple' "
        "   → marca=null, não 'Apple').\n"
        "4. Em caso de dúvida, prefira null a um valor incerto."
    )

    async def _extract(self, system: str, user_msg: str) -> dict:
        """Extração base — use _extract_validated() nas etapas de negócio."""
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system + self._EXTRACT_GUARDRAIL},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            logger.error(f"Erro na extração LLM: {e}")
            return {}

    async def _extract_validated(
        self,
        system: str,
        user_msg: str,
        validators: dict,
        max_retries: int = 2,
    ) -> dict:
        """
        Extração com guardrails:
          - Exige campo 'evidencia_<campo>' em cada campo extraído
          - Verifica que a evidência é um substring real da mensagem do usuário
          - Aplica validators[campo](value) para cada campo — retorna None se inválido
          - Retry com feedback de erro (max_retries tentativas)

        validators: {campo: callable(value) -> value_validado | None}
        """
        messages = [
            {"role": "system", "content": system + self._EXTRACT_GUARDRAIL},
            {"role": "user", "content": user_msg},
        ]
        last_result: dict = {}

        for tentativa in range(max_retries):
            try:
                resp = await self._get_client().chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=500,
                    response_format={"type": "json_object"},
                )
                raw = json.loads(resp.choices[0].message.content or "{}")
            except Exception as e:
                logger.error(f"Extração validada falhou (tentativa {tentativa+1}): {e}")
                break

            erros: list[str] = []
            resultado: dict = {}
            user_lower = user_msg.lower()

            for campo, validator in validators.items():
                valor_bruto = raw.get(campo)
                evidencia = raw.get(f"evidencia_{campo}")

                # Guardrail 1: evidência deve existir como substring real (só valida se há valor não-nulo)
                if valor_bruto is not None:
                    if evidencia is None or str(evidencia).lower() not in user_lower:
                        erros.append(
                            f"Campo '{campo}': valor '{valor_bruto}' extraído sem evidência textual na mensagem do usuário. "
                            f"Retorne null para '{campo}' e null para 'evidencia_{campo}'."
                        )
                        resultado[campo] = None
                        continue

                # Guardrail 2: validator de tipo/enum/range
                valor_validado = validator(valor_bruto)
                if valor_bruto is not None and valor_validado is None:
                    erros.append(
                        f"Campo '{campo}': valor '{valor_bruto}' inválido. "
                        f"Retorne null ou um dos valores permitidos. Inclua 'evidencia_{campo}' com o trecho exato."
                    )
                resultado[campo] = valor_validado

            last_result = resultado

            if not erros:
                return resultado

            # Retry com feedback dos erros (mantém palavra 'json' para response_format)
            logger.warning(f"Extração com erros (tentativa {tentativa+1}): {erros}")
            messages.append({"role": "assistant", "content": json.dumps(raw)})
            messages.append({
                "role": "user",
                "content": (
                    "Sua extração contém problemas. Corrija e retorne um novo JSON válido:\n"
                    + "\n".join(f"- {e}" for e in erros)
                ),
            })

        return last_result

    # ─────────────────────────────────────────────
    # Validators reutilizáveis
    # ─────────────────────────────────────────────

    @staticmethod
    def _val_condicao(v):
        validos = {"como_novo", "bom", "conservado", "desgastado", "com_defeito"}
        return v if isinstance(v, str) and v in validos else None

    @staticmethod
    def _val_estado_uso(v):
        return v if isinstance(v, str) and v in {"novo", "usado"} else None

    @staticmethod
    def _val_bool(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            if v.lower() in ("true", "sim", "yes", "1"):
                return True
            if v.lower() in ("false", "não", "nao", "no", "0"):
                return False
        return None

    @staticmethod
    def _val_price(v):
        """Preço deve ser número positivo entre R$1 e R$9.999.999."""
        try:
            f = float(v)
            if 1.0 <= f <= 9_999_999.0:
                return round(f, 2)
        except (TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _val_str_or_none(v):
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    @staticmethod
    def _val_pronto(v):
        if isinstance(v, bool):
            return v
        return None

    async def _reply(self, instrucao: str) -> str:
        """
        Gera resposta conversacional a partir de uma instrução de roteiro.
        O LLM só pode redigir a mensagem — não decide dados, não inventa informações.
        """
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é o NOTHA, assistente de venda de produtos via WhatsApp. "
                            "Sua única função aqui é redigir a mensagem descrita na instrução, "
                            "de forma curta, direta e natural. Máximo 3 frases. Sem markdown. "
                            "Português brasileiro coloquial.\n\n"
                            "PROIBIDO:\n"
                            "- Inventar ou inferir dados que não estão na instrução\n"
                            "- Sugerir preços ou valores não fornecidos\n"
                            "- Fazer perguntas além do que a instrução pede\n"
                            "- Começar com saudações (Oi!, Olá!, Certo!, Perfeito!)\n"
                            "- Dar informações sobre o produto além das fornecidas\n"
                            "- Prometer funcionalidades ou prazos não confirmados"
                        ),
                    },
                    {"role": "user", "content": instrucao},
                ],
                temperature=0.4,
                max_tokens=250,
            )
            return resp.choices[0].message.content or instrucao
        except Exception:
            return instrucao

    # ─────────────────────────────────────────────
    # Entrada do fluxo
    # ─────────────────────────────────────────────

    async def start(self) -> str:
        return await self._reply(
            "Inicie o cadastro perguntando o que o usuário quer vender. "
            "Uma pergunta simples e direta, no máximo uma frase."
        )

    # ─────────────────────────────────────────────
    # Dispatcher principal
    # ─────────────────────────────────────────────

    async def handle_message(
        self,
        flow: dict,
        text: str,
        seller_profile=None,
        db=None,
    ) -> tuple[dict, list, str, bool]:
        """
        Processa mensagem de texto no fluxo de cadastro.

        Retorna: (dados, fotos, resposta, concluido)
          - concluido=True quando o step é 'confirmar' e usuário confirmou
        """
        step = flow["step"]
        dados = _parse_jsonb(flow.get("dados"), {})
        fotos = _parse_jsonb(flow.get("fotos"), [])

        handlers = {
            "produto":      self._step_produto,
            "marca_modelo": self._step_marca_modelo,
            "estado_uso":   self._step_estado_uso,
            "condicao":     self._step_condicao,
            "nota_fiscal":  self._step_nota_fiscal,
            "fotos":        self._step_fotos_texto,
            "endereco":     self._step_endereco,
            "preco":        self._step_preco,
            "confirmar":    self._step_confirmar,
        }

        handler = handlers.get(step)
        if not handler:
            return dados, fotos, "Tudo certo! Pode continuar.", False

        if step in ("fotos", "endereco"):
            return await handler(dados, fotos, text, seller_profile)
        elif step == "preco":
            return await handler(dados, fotos, text, db)
        else:
            return await handler(dados, fotos, text)

    async def handle_media(
        self,
        flow: dict,
        media_id: str,
        mime_type: str,
        caption: str,
    ) -> tuple[list, str]:
        """
        Processa mídia recebida. Só aceita fotos durante o step 'fotos'.
        Retorna (fotos_atualizadas, resposta).
        """
        step = flow["step"]
        fotos = _parse_jsonb(flow.get("fotos"), [])

        if step != "fotos":
            return fotos, ""

        fotos.append({"media_id": media_id, "mime_type": mime_type, "caption": caption or ""})
        n = len(fotos)
        if n == 1:
            reply = await self._reply(
                "Recebi a primeira foto do produto! "
                "Diga que pode mandar mais fotos de outros ângulos ou da etiqueta/embalagem. "
                "Quando terminar é só digitar 'pronto'."
            )
        else:
            reply = await self._reply(
                f"Recebida a foto {n}! Pode mandar mais ou digitar 'pronto' quando terminar."
            )
        return fotos, reply

    # ─────────────────────────────────────────────
    # Handlers de cada etapa
    # ─────────────────────────────────────────────

    async def _step_produto(self, dados, fotos, text):
        dados["descricao"] = text.strip()
        pergunta = await self._reply(
            f"O usuário quer vender: '{text}'. "
            "Agora pergunte a marca, modelo e versão (se aplicável). "
            "Exemplo de resposta esperada: 'iPhone 13 Pro, 256GB' ou 'Nike Air Max 90'. "
            "Se não tiver marca/modelo, pode responder 'sem marca' ou 'não sei'."
        )
        dados["step_next"] = "marca_modelo"
        return dados, fotos, pergunta, False

    async def _step_marca_modelo(self, dados, fotos, text):
        ext = await self._extract_validated(
            system=(
                "Extraia marca, modelo e versão do texto.\n"
                "Retorne JSON com os campos: marca, modelo, versao.\n"
                "Exemplos:\n"
                "  'iPhone 13 Pro 256GB' → marca=null (usuário não disse 'Apple'), modelo='iPhone 13 Pro', versao='256GB'\n"
                "  'Nike Air Max 90' → marca='Nike', modelo='Air Max 90', versao=null\n"
                "  'sem marca' ou 'não sei' → todos null\n"
                "Se o usuário não mencionou a marca explicitamente, retorne marca=null. "
                "Não complete com marcas que você 'sabe' — só o que foi dito."
            ),
            user_msg=text,
            validators={
                "marca":  self._val_str_or_none,
                "modelo": self._val_str_or_none,
                "versao": self._val_str_or_none,
            },
        )
        dados.update({
            "marca":  ext.get("marca"),
            "modelo": ext.get("modelo"),
            "versao": ext.get("versao"),
        })
        pergunta = await self._reply(
            "Pergunte se o produto é novo (nunca usado, pode estar na caixa) ou usado."
        )
        dados["step_next"] = "estado_uso"
        return dados, fotos, pergunta, False

    async def _step_estado_uso(self, dados, fotos, text):
        ext = await self._extract_validated(
            system=(
                "Determine se o produto é novo ou usado com base EXCLUSIVAMENTE no que o usuário disse.\n"
                "Valores permitidos para 'estado_uso': 'novo' ou 'usado'.\n"
                "Palavras que indicam novo: novo, nunca usado, lacrado, na caixa, zerado.\n"
                "Na dúvida, retorne null — não assuma 'usado' automaticamente."
            ),
            user_msg=text,
            validators={"estado_uso": self._val_estado_uso},
        )
        dados["estado_uso"] = ext.get("estado_uso") or "usado"
        opcoes = "\n".join(f"  {i+1}. {v}" for i, v in enumerate(CONDICAO_LABEL.values()))
        pergunta = await self._reply(
            f"Produto declarado como {dados['estado_uso']}. "
            "Agora pergunte sobre o estado de conservação. As opções são:\n"
            f"{opcoes}\n"
            "Peça que o usuário escolha um número ou descreva com as próprias palavras."
        )
        dados["step_next"] = "condicao"
        return dados, fotos, pergunta, False

    async def _step_condicao(self, dados, fotos, text):
        ext = await self._extract_validated(
            system=(
                "Classifique o estado de conservação com base APENAS no que o usuário disse.\n"
                "Valores permitidos para 'condicao': como_novo, bom, conservado, desgastado, com_defeito.\n"
                "Mapeamento de números: 1=como_novo, 2=bom, 3=conservado, 4=desgastado, 5=com_defeito.\n"
                "Em 'descricao_condicao', copie literalmente as palavras do usuário — não parafraseie.\n"
                "Se a mensagem for ambígua, retorne condicao=null."
            ),
            user_msg=text,
            validators={
                "condicao":           self._val_condicao,
                "descricao_condicao": self._val_str_or_none,
            },
        )
        dados["condicao"] = ext.get("condicao") or "conservado"
        dados["descricao_condicao"] = ext.get("descricao_condicao") or text.strip()
        pergunta = await self._reply("Pergunte se o produto tem nota fiscal.")
        dados["step_next"] = "nota_fiscal"
        return dados, fotos, pergunta, False

    async def _step_nota_fiscal(self, dados, fotos, text):
        ext = await self._extract_validated(
            system=(
                "O usuário está informando se o produto tem nota fiscal.\n"
                "Extraia apenas o campo 'tem_nota_fiscal' (true ou false).\n"
                "Palavras que indicam 'tem': tem, sim, tenho, possui, veio com, inclui.\n"
                "Palavras que indicam 'não tem': não, sem, perdi, não tenho, não tem.\n"
                "Se ambíguo, retorne null — não assuma false automaticamente."
            ),
            user_msg=text,
            validators={"tem_nota_fiscal": self._val_bool},
        )
        dados["tem_nota_fiscal"] = ext.get("tem_nota_fiscal") if ext.get("tem_nota_fiscal") is not None else False
        pergunta = await self._reply(
            "Instrua o usuário a enviar as fotos do produto agora. "
            "Diga que pode mandar várias fotos mostrando diferentes ângulos, "
            "e também pode fotografar etiqueta, embalagem ou nota fiscal se tiver. "
            "Quando terminar, é só digitar 'pronto'."
        )
        dados["step_next"] = "fotos"
        return dados, fotos, pergunta, False

    async def _step_fotos_texto(self, dados, fotos, text, seller_profile):
        """Texto recebido durante etapa de fotos — geralmente indica que terminou."""
        if not fotos:
            reply = await self._reply(
                "Ainda não recebi nenhuma foto. Por favor, mande pelo menos uma foto do produto para continuar!"
            )
            return dados, fotos, reply, False

        pronto = await self._extract_validated(
            system=(
                "O usuário está no processo de envio de fotos de um produto.\n"
                "Determine APENAS se a mensagem indica que terminou de enviar fotos.\n"
                "Campo 'pronto': true se a mensagem sinaliza conclusão, false se ainda quer enviar mais.\n"
                "Palavras que indicam conclusão: pronto, ok, é isso, terminei, pode seguir, continuar, acabou, só isso, encerrei.\n"
                "Se a mensagem for uma pergunta, comentário ou descrição — não é 'pronto'."
            ),
            user_msg=text,
            validators={"pronto": self._val_pronto},
        )
        if not pronto.get("pronto", True):
            dados["obs_fotos"] = text
            reply = await self._reply("Anotei! Tem mais fotos para enviar ou pode digitar 'pronto' para continuar.")
            return dados, fotos, reply, False

        endereco_existente = (seller_profile or {}).get("endereco_retirada")
        if endereco_existente:
            pergunta = await self._reply(
                f"Recebi {len(fotos)} foto(s)! "
                f"O endereço de retirada cadastrado é: {endereco_existente}. "
                "Pergunte se quer usar esse endereço ou informar um diferente para este produto."
            )
        else:
            pergunta = await self._reply(
                f"Recebi {len(fotos)} foto(s)! "
                "Agora preciso do endereço de retirada após a venda. "
                "Peça o endereço completo: rua, número, bairro, cidade e CEP."
            )
        dados["_endereco_sugerido"] = endereco_existente
        dados["step_next"] = "endereco"
        return dados, fotos, pergunta, False

    async def _step_endereco(self, dados, fotos, text, seller_profile):
        endereco_sugerido = dados.get("_endereco_sugerido")
        if endereco_sugerido:
            ext = await self._extract_validated(
                system=(
                    "O usuário foi perguntado se confirma o endereço cadastrado ou quer informar um novo.\n"
                    "Extraia:\n"
                    "  'confirma_existente': true se o usuário aceitou o endereço já cadastrado.\n"
                    "  'novo_endereco': string com o novo endereço, ou null se não forneceu um novo.\n"
                    "Palavras de confirmação: sim, pode usar, esse mesmo, o cadastrado, tá bom, ok.\n"
                    "NUNCA invente um endereço — se o usuário não forneceu texto de endereço, novo_endereco=null."
                ),
                user_msg=text,
                validators={
                    "confirma_existente": self._val_bool,
                    "novo_endereco":      self._val_str_or_none,
                },
            )
            if ext.get("confirma_existente"):
                dados["endereco_retirada"] = endereco_sugerido
            elif ext.get("novo_endereco"):
                dados["endereco_retirada"] = ext["novo_endereco"]
            else:
                dados["endereco_retirada"] = text.strip()
        else:
            dados["endereco_retirada"] = text.strip()

        pergunta = await self._reply(
            "Agora pergunte qual o preço de venda que o vendedor quer anunciar "
            "e qual o valor mínimo que aceitaria. "
            "Explique que o mínimo é sigiloso e nunca será revelado ao comprador."
        )
        dados["step_next"] = "preco"
        return dados, fotos, pergunta, False

    async def _step_preco(self, dados, fotos, text, db):
        ext = await self._extract_validated(
            system=(
                "O usuário está informando o preço de venda e/ou o preço mínimo que aceitaria.\n"
                "Extraia:\n"
                "  'preco_desejado': valor numérico em reais (ex: 'quero 500' → 500.0), ou null.\n"
                "  'preco_minimo_vendedor': valor numérico do mínimo aceitável (ex: 'aceito no mínimo 400' → 400.0), ou null.\n"
                "Valores por extenso são aceitos: 'quinhentos reais' → 500.\n"
                "NUNCA invente um preço mínimo se o usuário não mencionou. "
                "NUNCA arredonde ou ajuste o valor — use exatamente o que o usuário disse."
            ),
            user_msg=text,
            validators={
                "preco_desejado":        self._val_price,
                "preco_minimo_vendedor": self._val_price,
            },
        )
        dados["preco_desejado"] = ext.get("preco_desejado")
        dados["preco_minimo_vendedor"] = ext.get("preco_minimo_vendedor")
        dados["step_next"] = "processando"
        reply = await self._reply(
            "Diga que recebeu tudo e que agora vai pesquisar o produto na internet e no histórico "
            "da plataforma para sugerir o melhor preço. Diga que isso leva alguns segundos."
        )
        return dados, fotos, reply, False

    async def _step_confirmar(self, dados, fotos, text):
        ext = await self._extract_validated(
            system=(
                "O usuário está respondendo ao resumo do anúncio para confirmar ou rejeitar.\n"
                "Extraia:\n"
                "  'confirmou': true se o usuário aceitou e quer publicar, false se recusou ou quer mudar algo.\n"
                "  'novo_preco': valor numérico se o usuário pediu explicitamente para anunciar por outro preço, null caso contrário.\n"
                "Confirmações claras: sim, confirmo, pode anunciar, fechou, ok, tá bom, isso mesmo, pode publicar.\n"
                "Recusas: não, mudei de ideia, quero mudar, cancela, espera.\n"
                "NUNCA deduza 'confirmou=true' se a mensagem for ambígua. Na dúvida, confirmou=false."
            ),
            user_msg=text,
            validators={
                "confirmou":  self._val_bool,
                "novo_preco": self._val_price,
            },
        )
        if ext.get("confirmou") is True:
            dados["confirmado"] = True
            dados["step_next"] = "concluido"
            return dados, fotos, "", True

        novo_preco = ext.get("novo_preco")
        if novo_preco:
            dados["preco_anunciado"] = novo_preco
            reply = await self._reply(
                f"O usuário quer anunciar por R$ {novo_preco:.2f}. "
                "Confirme a alteração e pergunte se quer publicar com esse preço."
            )
        else:
            reply = await self._reply(
                "O usuário não confirmou. Pergunte o que gostaria de ajustar no anúncio."
            )
        return dados, fotos, reply, False

    # ─────────────────────────────────────────────
    # Processamento automático (step: processando)
    # ─────────────────────────────────────────────

    async def processar(
        self,
        flow: dict,
        listing_repo=None,
        db=None,
    ) -> tuple[dict, str]:
        """
        Executa o processamento completo:
          1. Busca web: preços de mercado + ficha técnica do produto
          2. Histórico do banco: vendas similares (por categoria)
          3. Análise visual: GPT-4o Vision nas fotos enviadas
          4. PricingAgent: cruza tudo e gera preço sugerido + mínimo

        Retorna (dados_atualizados, mensagem_de_confirmação).
        """
        from agents.pricing import PricingAgent
        from tools.builtin.web_search import WebSearchTool

        dados = _parse_jsonb(flow.get("dados"), {})
        fotos = _parse_jsonb(flow.get("fotos"), [])

        descricao         = dados.get("descricao", "")
        marca             = dados.get("marca") or ""
        modelo            = dados.get("modelo") or ""
        versao            = dados.get("versao") or ""
        condicao          = dados.get("condicao", "conservado")
        estado_uso        = dados.get("estado_uso", "usado")
        tem_nota_fiscal   = dados.get("tem_nota_fiscal", False)
        preco_desejado    = dados.get("preco_desejado")
        preco_min_vend    = dados.get("preco_minimo_vendedor")
        endereco          = dados.get("endereco_retirada", "")

        nome_produto = " ".join(filter(None, [marca, modelo, versao])) or descricao

        # 1. Busca web — preços + ficha técnica
        searcher = WebSearchTool()
        web_precos, web_specs = None, None
        try:
            web_precos = await searcher.execute(
                f"preço {nome_produto} usado site:olx.com.br OR site:mercadolivre.com.br"
            )
        except Exception as e:
            logger.warning(f"Busca de preços falhou: {e}")
        try:
            web_specs = await searcher.execute(f"{nome_produto} especificações ficha técnica")
        except Exception as e:
            logger.warning(f"Busca de specs falhou: {e}")

        dados["info_web"] = {
            "precos": (web_precos or "")[:600],
            "specs":  (web_specs  or "")[:400],
        }

        # 2. Histórico de vendas similares no banco
        historico_similares = []
        categoria = dados.get("categoria") or _inferir_categoria(nome_produto)
        dados["categoria"] = categoria
        if db and listing_repo:
            try:
                rows = await listing_repo.find_similar_sold(categoria)
                historico_similares = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"Histórico banco falhou: {e}")

        # 3. Download das imagens como base64 (uma única vez — usado pela análise e pelo pricing)
        base64_images: list[str] = []
        if fotos:
            from whatsapp import download_media_as_base64
            for foto in fotos[:4]:
                data_uri = await download_media_as_base64(
                    foto.get("media_id", ""),
                    foto.get("mime_type", "image/jpeg"),
                )
                if data_uri:
                    base64_images.append(data_uri)
            logger.info(f"Download de {len(base64_images)}/{len(fotos[:4])} fotos para análise visual")

        # 3b. Análise visual das fotos (GPT-4o Vision)
        vision_result = None
        if base64_images:
            vision_result = await self._analisar_fotos(
                fotos, nome_produto, condicao, base64_images=base64_images
            )
        dados["vision_analysis"] = vision_result

        # 4. Precificação com todos os dados (+ imagens para contexto visual no pricing)
        descricao_rica = (
            f"{nome_produto}. "
            f"Estado: {estado_uso}. "
            f"Condição: {CONDICAO_LABEL.get(condicao, condicao)}. "
            f"Nota fiscal: {'sim' if tem_nota_fiscal else 'não'}. "
            + (f"Análise visual: {vision_result}. " if vision_result else "")
            + (f"Preços encontrados na web: {web_precos[:300]}." if web_precos else "")
        )
        pricing_agent = PricingAgent(db)
        appraisal = await pricing_agent.appraise(
            descricao=descricao_rica,
            categoria=categoria,
            preco_informado_vendedor=preco_desejado,
            historico_similares=historico_similares,
            fotos=base64_images or None,
        )
        dados["appraisal"] = appraisal

        # Cidade para contexto geográfico
        dados["cidade_vendedor"] = _extrair_cidade(endereco)

        # Decide preço anunciado e mínimo do sistema
        preco_agente   = appraisal.get("preco_sugerido", 0) or 0
        minimo_agente  = appraisal.get("preco_minimo_sugerido", 0) or 0
        justificativa  = appraisal.get("justificativa", "")
        confianca      = appraisal.get("confianca", "baixa")

        preco_anunciado = preco_desejado or preco_agente
        preco_minimo    = preco_min_vend or minimo_agente

        dados["preco_anunciado"]     = preco_anunciado
        dados["preco_minimo"]        = preco_minimo
        dados["step_next"]           = "confirmar"

        # Alerta de discrepância de preço
        alerta_preco = ""
        if preco_desejado and preco_agente > 0:
            diff = abs(preco_desejado - preco_agente) / preco_agente
            if diff > 0.30:
                direcao = "acima" if preco_desejado > preco_agente else "abaixo"
                alerta_preco = (
                    f"Atenção: seu preço de R$ {preco_desejado:.2f} está "
                    f"{diff*100:.0f}% {direcao} do valor de mercado de R$ {preco_agente:.2f}. "
                )

        # Monta mensagem de confirmação
        linhas = [
            f"Produto: {nome_produto}",
            f"Estado: {estado_uso} | Condição: {CONDICAO_LABEL.get(condicao, condicao)}",
            f"Nota fiscal: {'sim' if tem_nota_fiscal else 'não'}",
            f"Fotos: {len(fotos)} enviada(s)",
            f"Retirada: {endereco or 'não informado'}",
        ]
        if preco_desejado:
            linhas.append(f"Seu preço: R$ {preco_desejado:.2f}")
        if preco_min_vend:
            linhas.append(f"Seu mínimo: R$ {preco_min_vend:.2f} (sigiloso)")
        linhas.append(f"Avaliação NOTHA: R$ {preco_agente:.2f} (confiança: {confianca})")
        linhas.append(f"Motivo: {justificativa}")
        if alerta_preco:
            linhas.append(alerta_preco)
        linhas.append(f"Será anunciado por: R$ {preco_anunciado:.2f}")

        resumo = "\n".join(linhas)
        msg = await self._reply(
            f"Apresente o resumo do anúncio e pergunte se confirma:\n\n{resumo}"
        )
        return dados, msg

    async def _analisar_fotos(
        self, fotos: list, produto: str, condicao_declarada: str,
        base64_images: list[str] | None = None,
    ) -> str | None:
        """
        Usa GPT-4o Vision para analisar as fotos e verificar a condição declarada.

        Aceita base64_images (lista de data URIs já baixadas) para evitar download duplo.
        Se não fornecido, baixa cada foto do WhatsApp como base64 internamente.
        URLs diretas do WhatsApp não funcionam com GPT-4o — exigem Authorization header.
        """
        from whatsapp import download_media_as_base64

        imagens = base64_images or []
        if not imagens:
            for foto in fotos[:4]:
                data_uri = await download_media_as_base64(
                    foto.get("media_id", ""),
                    foto.get("mime_type", "image/jpeg"),
                )
                if data_uri:
                    imagens.append(data_uri)

        content = []
        for data_uri in imagens:
            content.append({"type": "image_url", "image_url": {"url": data_uri, "detail": "low"}})

        if not content:
            return None

        content.append({
            "type": "text",
            "text": (
                f"Produto: {produto}. Condição declarada pelo vendedor: {CONDICAO_LABEL.get(condicao_declarada, condicao_declarada)}.\n\n"
                "REGRAS DA ANÁLISE (obrigatórias):\n"
                "1. Descreva APENAS o que é visualmente observável nas imagens — não infira especificações técnicas, modelos, preços ou procedência.\n"
                "2. Liste objetivamente: acabamento, arranhões, manchas, amassados, desgaste ou danos visíveis.\n"
                "3. Diga se a condição declarada é CONSISTENTE ou INCONSISTENTE com o que aparece nas fotos — justifique com o que você viu.\n"
                "4. NÃO atribua valor de mercado. NÃO sugira preços. NÃO faça afirmações sobre autenticidade.\n"
                "5. Se as fotos estiverem desfocadas, escuras ou insuficientes para avaliar, diga isso explicitamente em vez de adivinhar.\n"
                "Responda em 2-4 frases objetivas."
            ),
        })
        try:
            resp = await self._get_client().chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": content}],
                max_tokens=250,
                temperature=0.0,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"Análise visual falhou: {e}")
            return None


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _inferir_categoria(nome: str) -> str:
    n = nome.lower()
    mapa = {
        "eletrônicos": [
            "iphone", "samsung", "celular", "smartphone", "notebook", "computador",
            "tablet", "ipad", "tv", "monitor", "fone", "headphone", "console",
            "playstation", "xbox", "nintendo", "câmera", "camera",
        ],
        "eletrodomésticos": [
            "geladeira", "fogão", "micro-ondas", "lavadora", "máquina de lavar",
            "ar condicionado", "ventilador", "liquidificador", "batedeira", "churrasqueira",
        ],
        "móveis": [
            "sofá", "sofa", "mesa", "cadeira", "cama", "guarda-roupa", "armário",
            "estante", "escrivaninha", "rack",
        ],
        "vestuário": [
            "camisa", "camiseta", "calça", "vestido", "sapato", "tênis", "sandália",
            "casaco", "jaqueta", "bolsa", "mochila",
        ],
        "veículos": ["carro", "moto", "bicicleta", "patinete", "scooter"],
        "brinquedos": ["brinquedo", "boneca", "lego", "jogo de tabuleiro"],
        "esportes": ["esteira", "haltere", "peso", "raquete", "bola", "bike"],
        "livros": ["livro", "revista", "manual", "apostila"],
    }
    for cat, palavras in mapa.items():
        if any(p in n for p in palavras):
            return cat
    return "outros"


def _extrair_cidade(endereco: str) -> str | None:
    if not endereco:
        return None
    partes = [p.strip().rstrip(",") for p in endereco.split() if p.strip()]
    for i, parte in enumerate(partes):
        if len(parte) == 2 and parte.isupper() and i > 0:
            return partes[i - 1]
    if len(partes) >= 2:
        return partes[-2]
    return None

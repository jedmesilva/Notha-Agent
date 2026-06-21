"""
ListingFlowAgent — máquina de estados para cadastro completo de produto via WhatsApp.

Steps:
  product          → O que você quer vender?
  brand_model      → Marca, modelo e versão
  usage_state      → Novo ou usado?
  condition        → Estado de conservação
  receipt          → Tem nota fiscal?
  photos_upload    → Fotos do produto (múltiplas; texto = "pronto")
  address          → Endereço de retirada
  price            → Preço desejado e mínimo aceitável
  processing       → [automático] busca web + banco + visão + precificação
  review_condition → [condicional] pausa quando visão detecta condição inconsistente
  confirm          → Resumo e confirmação
  done             → Listing criado
"""
import json
import logging
from llm import get_provider

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


CONDITION_LABEL = {
    "like_new":  "Como novo (sem marcas de uso)",
    "good":      "Bom estado (uso leve, poucas marcas)",
    "fair":      "Conservado (uso normal, pequenos desgastes)",
    "worn":      "Desgastado (uso intenso, marcas visíveis)",
    "defective": "Com defeito (funciona parcialmente ou não funciona)",
}


class ListingFlowAgent:

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
        "   → brand=null, não 'Apple').\n"
        "4. Em caso de dúvida, prefira null a um valor incerto."
    )

    async def _extract(self, system: str, user_msg: str) -> dict:
        """Extração base — use _extract_validated() nas etapas de negócio."""
        try:
            resp = await get_provider().complete(
                messages=[
                    {"role": "system", "content": system + self._EXTRACT_GUARDRAIL},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=400,
                json_mode=True,
            )
            return json.loads(resp.text or "{}")
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
                resp = await get_provider().complete(
                    messages=messages,
                    temperature=0.0,
                    max_tokens=500,
                    json_mode=True,
                )
                raw = json.loads(resp.text or "{}")
            except Exception as e:
                logger.error(f"Extração validada falhou (tentativa {tentativa+1}): {e}")
                break

            erros: list[str] = []
            resultado: dict = {}
            user_lower = user_msg.lower()

            for campo, validator in validators.items():
                valor_bruto = raw.get(campo)
                evidencia = raw.get(f"evidencia_{campo}")

                if valor_bruto is not None:
                    if evidencia is None or str(evidencia).lower() not in user_lower:
                        erros.append(
                            f"Campo '{campo}': valor '{valor_bruto}' extraído sem evidência textual na mensagem do usuário. "
                            f"Retorne null para '{campo}' e null para 'evidencia_{campo}'."
                        )
                        resultado[campo] = None
                        continue

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
    def _val_condition(v):
        valid = {"like_new", "good", "fair", "worn", "defective"}
        return v if isinstance(v, str) and v in valid else None

    @staticmethod
    def _val_usage_state(v):
        return v if isinstance(v, str) and v in {"new", "used"} else None

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
    def _val_ready(v):
        if isinstance(v, bool):
            return v
        return None

    async def _reply(self, instrucao: str) -> str:
        """
        Gera resposta conversacional a partir de uma instrução de roteiro.
        O LLM só pode redigir a mensagem — não decide dados, não inventa informações.
        """
        try:
            resp = await get_provider().complete(
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
            return resp.text or instrucao
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

        Retorna: (data, photos, resposta, completed)
          - completed=True quando o step é 'confirm' e usuário confirmou
        """
        step = flow["step"]
        data   = _parse_jsonb(flow.get("data"), {})
        photos = _parse_jsonb(flow.get("photos"), [])

        handlers = {
            "product":          self._step_product,
            "brand_model":      self._step_brand_model,
            "usage_state":      self._step_usage_state,
            "condition":        self._step_condition,
            "receipt":          self._step_receipt,
            "photos_upload":    self._step_photos_text,
            "address":          self._step_address,
            "price":            self._step_price,
            "review_condition": self._step_review_condition,
            "confirm":          self._step_confirm,
        }

        handler = handlers.get(step)
        if not handler:
            return data, photos, "Tudo certo! Pode continuar.", False

        if step in ("photos_upload", "address"):
            return await handler(data, photos, text, seller_profile)
        elif step == "price":
            return await handler(data, photos, text, db)
        else:
            return await handler(data, photos, text)

    async def handle_media(
        self,
        flow: dict,
        media_id: str,
        mime_type: str,
        caption: str,
    ) -> tuple[list, str]:
        """
        Processa mídia recebida. Só aceita fotos durante o step 'photos_upload'.
        Retorna (photos_updated, resposta).
        """
        step   = flow["step"]
        photos = _parse_jsonb(flow.get("photos"), [])

        if step != "photos_upload":
            return photos, ""

        photos.append({"media_id": media_id, "mime_type": mime_type, "caption": caption or ""})
        n = len(photos)
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
        return photos, reply

    # ─────────────────────────────────────────────
    # Handlers de cada etapa
    # ─────────────────────────────────────────────

    async def _step_product(self, data, photos, text):
        data["description"] = text.strip()
        pergunta = await self._reply(
            f"O usuário quer vender: '{text}'. "
            "Agora pergunte a marca, modelo e versão (se aplicável). "
            "Exemplo de resposta esperada: 'iPhone 13 Pro, 256GB' ou 'Nike Air Max 90'. "
            "Se não tiver marca/modelo, pode responder 'sem marca' ou 'não sei'."
        )
        data["step_next"] = "brand_model"
        return data, photos, pergunta, False

    async def _step_brand_model(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "Extraia marca, modelo e versão do texto.\n"
                "Retorne JSON com os campos: brand, model, version.\n"
                "Exemplos:\n"
                "  'iPhone 13 Pro 256GB' → brand=null (usuário não disse 'Apple'), model='iPhone 13 Pro', version='256GB'\n"
                "  'Nike Air Max 90' → brand='Nike', model='Air Max 90', version=null\n"
                "  'sem marca' ou 'não sei' → todos null\n"
                "Se o usuário não mencionou a marca explicitamente, retorne brand=null. "
                "Não complete com marcas que você 'sabe' — só o que foi dito."
            ),
            user_msg=text,
            validators={
                "brand":   self._val_str_or_none,
                "model":   self._val_str_or_none,
                "version": self._val_str_or_none,
            },
        )
        data.update({
            "brand":   ext.get("brand"),
            "model":   ext.get("model"),
            "version": ext.get("version"),
        })
        pergunta = await self._reply(
            "Pergunte se o produto é novo (nunca usado, pode estar na caixa) ou usado."
        )
        data["step_next"] = "usage_state"
        return data, photos, pergunta, False

    async def _step_usage_state(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "Determine se o produto é novo ou usado com base EXCLUSIVAMENTE no que o usuário disse.\n"
                "Valores permitidos para 'usage_state': 'new' ou 'used'.\n"
                "Palavras que indicam new: novo, nunca usado, lacrado, na caixa, zerado.\n"
                "Na dúvida, retorne null — não assuma 'used' automaticamente."
            ),
            user_msg=text,
            validators={"usage_state": self._val_usage_state},
        )
        data["usage_state"] = ext.get("usage_state") or "used"
        opcoes = "\n".join(f"  {i+1}. {v}" for i, v in enumerate(CONDITION_LABEL.values()))
        pergunta = await self._reply(
            f"Produto declarado como {data['usage_state']}. "
            "Agora pergunte sobre o estado de conservação. As opções são:\n"
            f"{opcoes}\n"
            "Peça que o usuário escolha um número ou descreva com as próprias palavras."
        )
        data["step_next"] = "condition"
        return data, photos, pergunta, False

    async def _step_condition(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "Classifique o estado de conservação com base APENAS no que o usuário disse.\n"
                "Valores permitidos para 'condition': like_new, good, fair, worn, defective.\n"
                "Mapeamento de números: 1=like_new, 2=good, 3=fair, 4=worn, 5=defective.\n"
                "Em 'condition_description', copie literalmente as palavras do usuário — não parafraseie.\n"
                "Se a mensagem for ambígua, retorne condition=null."
            ),
            user_msg=text,
            validators={
                "condition":             self._val_condition,
                "condition_description": self._val_str_or_none,
            },
        )
        data["condition"]             = ext.get("condition") or "fair"
        data["condition_description"] = ext.get("condition_description") or text.strip()
        pergunta = await self._reply("Pergunte se o produto tem nota fiscal.")
        data["step_next"] = "receipt"
        return data, photos, pergunta, False

    async def _step_receipt(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "O usuário está informando se o produto tem nota fiscal.\n"
                "Extraia apenas o campo 'has_receipt' (true ou false).\n"
                "Palavras que indicam 'tem': tem, sim, tenho, possui, veio com, inclui.\n"
                "Palavras que indicam 'não tem': não, sem, perdi, não tenho, não tem.\n"
                "Se ambíguo, retorne null — não assuma false automaticamente."
            ),
            user_msg=text,
            validators={"has_receipt": self._val_bool},
        )
        data["has_receipt"] = ext.get("has_receipt") if ext.get("has_receipt") is not None else False
        pergunta = await self._reply(
            "Instrua o usuário a enviar as fotos do produto agora. "
            "Diga que pode mandar várias fotos mostrando diferentes ângulos, "
            "e também pode fotografar etiqueta, embalagem ou nota fiscal se tiver. "
            "Quando terminar, é só digitar 'pronto'."
        )
        data["step_next"] = "photos_upload"
        return data, photos, pergunta, False

    async def _step_photos_text(self, data, photos, text, seller_profile):
        """Texto recebido durante etapa de fotos — geralmente indica que terminou."""
        if not photos:
            reply = await self._reply(
                "Ainda não recebi nenhuma foto. Por favor, mande pelo menos uma foto do produto para continuar!"
            )
            return data, photos, reply, False

        ready = await self._extract_validated(
            system=(
                "O usuário está no processo de envio de fotos de um produto.\n"
                "Determine APENAS se a mensagem indica que terminou de enviar fotos.\n"
                "Campo 'ready': true se a mensagem sinaliza conclusão, false se ainda quer enviar mais.\n"
                "Palavras que indicam conclusão: pronto, ok, é isso, terminei, pode seguir, continuar, acabou, só isso, encerrei.\n"
                "Se a mensagem for uma pergunta, comentário ou descrição — não é 'ready'."
            ),
            user_msg=text,
            validators={"ready": self._val_ready},
        )
        if not ready.get("ready", True):
            data["photo_notes"] = text
            reply = await self._reply("Anotei! Tem mais fotos para enviar ou pode digitar 'pronto' para continuar.")
            return data, photos, reply, False

        pickup_address = (seller_profile or {}).get("pickup_address")
        if pickup_address:
            pergunta = await self._reply(
                f"Recebi {len(photos)} foto(s)! "
                f"O endereço de retirada cadastrado é: {pickup_address}. "
                "Pergunte se quer usar esse endereço ou informar um diferente para este produto."
            )
        else:
            pergunta = await self._reply(
                f"Recebi {len(photos)} foto(s)! "
                "Agora preciso do endereço de retirada após a venda. "
                "Peça o endereço completo: rua, número, bairro, cidade e CEP."
            )
        data["_suggested_address"] = pickup_address
        data["step_next"] = "address"
        return data, photos, pergunta, False

    async def _step_address(self, data, photos, text, seller_profile):
        suggested = data.get("_suggested_address")
        if suggested:
            ext = await self._extract_validated(
                system=(
                    "O usuário foi perguntado se confirma o endereço cadastrado ou quer informar um novo.\n"
                    "Extraia:\n"
                    "  'confirms_existing': true se o usuário aceitou o endereço já cadastrado.\n"
                    "  'new_address': string com o novo endereço, ou null se não forneceu um novo.\n"
                    "Palavras de confirmação: sim, pode usar, esse mesmo, o cadastrado, tá bom, ok.\n"
                    "NUNCA invente um endereço — se o usuário não forneceu texto de endereço, new_address=null."
                ),
                user_msg=text,
                validators={
                    "confirms_existing": self._val_bool,
                    "new_address":       self._val_str_or_none,
                },
            )
            if ext.get("confirms_existing"):
                data["pickup_address"] = suggested
            elif ext.get("new_address"):
                data["pickup_address"] = ext["new_address"]
            else:
                data["pickup_address"] = text.strip()
        else:
            data["pickup_address"] = text.strip()

        pergunta = await self._reply(
            "Agora pergunte qual o preço de venda que o vendedor quer anunciar "
            "e qual o valor mínimo que aceitaria. "
            "Explique que o mínimo é sigiloso e nunca será revelado ao comprador."
        )
        data["step_next"] = "price"
        return data, photos, pergunta, False

    async def _step_price(self, data, photos, text, db):
        ext = await self._extract_validated(
            system=(
                "O usuário está informando o preço de venda e/ou o preço mínimo que aceitaria.\n"
                "Extraia:\n"
                "  'asking_price': valor numérico em reais (ex: 'quero 500' → 500.0), ou null.\n"
                "  'seller_min_price': valor numérico do mínimo aceitável (ex: 'aceito no mínimo 400' → 400.0), ou null.\n"
                "Valores por extenso são aceitos: 'quinhentos reais' → 500.\n"
                "NUNCA invente um preço mínimo se o usuário não mencionou. "
                "NUNCA arredonde ou ajuste o valor — use exatamente o que o usuário disse."
            ),
            user_msg=text,
            validators={
                "asking_price":    self._val_price,
                "seller_min_price": self._val_price,
            },
        )
        data["asking_price"]    = ext.get("asking_price")
        data["seller_min_price"] = ext.get("seller_min_price")
        data["step_next"] = "processing"
        reply = await self._reply(
            "Diga que recebeu tudo e que agora vai pesquisar o produto na internet e no histórico "
            "da plataforma para sugerir o melhor preço. Diga que isso leva alguns segundos."
        )
        return data, photos, reply, False

    async def _step_review_condition(self, data, photos, text):
        """
        Pausa ativada quando a análise visual detecta inconsistência com a condição declarada.

        Mostra ao vendedor o que foi detectado nas fotos e oferece duas opções:
          1. Manter a condição declarada (ele confirma que está certo)
          2. Corrigir a condição (escolhe uma das 5 opções)

        Só avança para 'confirm' após uma resposta válida.
        """
        vision_data = _parse_jsonb(data.get("vision_analysis"), {})
        visual_desc = vision_data.get("descricao_visual", "") if vision_data else ""
        current_condition = data.get("condition", "fair")

        valid_options = list(CONDITION_LABEL.keys())
        options_text = "\n".join(
            f"  {i+1}. {v}" for i, v in enumerate(CONDITION_LABEL.values())
        )

        ext = await self._extract_validated(
            system=(
                "O vendedor está respondendo sobre o estado de conservação do produto.\n"
                "Ele pode estar confirmando a condição já declarada ou corrigindo para uma nova.\n\n"
                "Extraia:\n"
                "  'kept': true se confirmou manter a condição atual, false se quer corrigir.\n"
                "  'new_condition': valor da nova condição se ele corrigiu, null se manteve.\n"
                f"Valores válidos para 'new_condition': {', '.join(valid_options)}.\n"
                "Mapeamento de números: 1=like_new, 2=good, 3=fair, 4=worn, 5=defective.\n"
                "Palavras de confirmação: sim, mantenho, está correto, pode deixar, é isso mesmo.\n"
                "Se ambíguo, kept=false e new_condition=null (pede nova resposta)."
            ),
            user_msg=text,
            validators={
                "kept":          self._val_bool,
                "new_condition": self._val_condition,
            },
        )

        kept          = ext.get("kept")
        new_condition = ext.get("new_condition")

        if kept is True:
            data["condition_revised"] = False
            data["step_next"] = "confirm"
            reply = await self._reply(
                f"O vendedor manteve a condição declarada: {CONDITION_LABEL.get(current_condition, current_condition)}. "
                "Diga que está registrado e que vamos seguir para o resumo do anúncio."
            )
            return data, photos, reply, False

        if new_condition:
            data["condition"]             = new_condition
            data["condition_description"] = f"Corrigido pelo vendedor após análise visual: {text.strip()}"
            data["condition_revised"]     = True
            data["step_next"] = "confirm"
            reply = await self._reply(
                f"O vendedor corrigiu a condição para: {CONDITION_LABEL.get(new_condition, new_condition)}. "
                "Confirme a correção e diga que vamos seguir para o resumo."
            )
            return data, photos, reply, False

        msg = await self._reply(
            f"Não entendi a resposta. Apresente as opções de condição e pergunte qual se aplica:\n"
            f"{options_text}\n"
            "Ou diga 'sim' para confirmar a condição já declarada."
        )
        return data, photos, msg, False

    async def _step_confirm(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "O usuário está respondendo ao resumo do anúncio para confirmar ou rejeitar.\n"
                "Extraia:\n"
                "  'confirmed': true se o usuário aceitou e quer publicar, false se recusou ou quer mudar algo.\n"
                "  'new_price': valor numérico se o usuário pediu explicitamente para anunciar por outro preço, null caso contrário.\n"
                "Confirmações claras: sim, confirmo, pode anunciar, fechou, ok, tá bom, isso mesmo, pode publicar.\n"
                "Recusas: não, mudei de ideia, quero mudar, cancela, espera.\n"
                "NUNCA deduza 'confirmed=true' se a mensagem for ambígua. Na dúvida, confirmed=false."
            ),
            user_msg=text,
            validators={
                "confirmed":  self._val_bool,
                "new_price":  self._val_price,
            },
        )
        if ext.get("confirmed") is True:
            data["confirmed"]  = True
            data["step_next"]  = "done"
            return data, photos, "", True

        new_price = ext.get("new_price")
        if new_price:
            data["listed_price"] = new_price
            reply = await self._reply(
                f"O usuário quer anunciar por R$ {new_price:.2f}. "
                "Confirme a alteração e pergunte se quer publicar com esse preço."
            )
        else:
            reply = await self._reply(
                "O usuário não confirmou. Pergunte o que gostaria de ajustar no anúncio."
            )
        return data, photos, reply, False

    # ─────────────────────────────────────────────
    # Processamento automático (step: processing)
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

        Retorna (data_atualizado, mensagem_de_confirmação).
        """
        from agents.pricing import PricingAgent
        from tools.builtin.web_search import WebSearchTool

        data   = _parse_jsonb(flow.get("data"), {})
        photos = _parse_jsonb(flow.get("photos"), [])

        description    = data.get("description", "")
        brand          = data.get("brand") or ""
        model          = data.get("model") or ""
        version        = data.get("version") or ""
        condition      = data.get("condition", "fair")
        usage_state    = data.get("usage_state", "used")
        has_receipt    = data.get("has_receipt", False)
        asking_price   = data.get("asking_price")
        seller_min     = data.get("seller_min_price")
        pickup_address = data.get("pickup_address", "")

        product_name = " ".join(filter(None, [brand, model, version])) or description

        # 1. Busca web — preços + ficha técnica
        searcher = WebSearchTool()
        web_prices, web_specs = None, None
        try:
            web_prices = await searcher.execute(
                f"preço {product_name} usado site:olx.com.br OR site:mercadolivre.com.br"
            )
        except Exception as e:
            logger.warning(f"Busca de preços falhou: {e}")
        try:
            web_specs = await searcher.execute(f"{product_name} especificações ficha técnica")
        except Exception as e:
            logger.warning(f"Busca de specs falhou: {e}")

        data["web_info"] = {
            "prices": (web_prices or "")[:600],
            "specs":  (web_specs  or "")[:400],
        }

        # 2. Histórico de vendas similares no banco
        similar_history = []
        category = data.get("category") or _infer_category(product_name)
        data["category"] = category
        if db and listing_repo:
            try:
                rows = await listing_repo.find_similar_sold(category)
                similar_history = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"Histórico banco falhou: {e}")

        # 3. Download das imagens como base64 (uma única vez)
        base64_images: list[str] = []
        if photos:
            from whatsapp import download_media_as_base64
            for photo in photos[:4]:
                data_uri = await download_media_as_base64(
                    photo.get("media_id", ""),
                    photo.get("mime_type", "image/jpeg"),
                )
                if data_uri:
                    base64_images.append(data_uri)
            logger.info(f"Download de {len(base64_images)}/{len(photos[:4])} fotos para análise visual")

        # 3b. Análise visual das fotos (GPT-4o Vision)
        vision_data: dict | None = None
        if base64_images:
            vision_data = await self._analyze_photos(
                photos, product_name, condition, base64_images=base64_images
            )
        data["vision_analysis"] = vision_data

        # 3c. Preenche campos vazios com dados extraídos visualmente
        if vision_data:
            filled = []
            if not data.get("brand") and vision_data.get("marca_visivel"):
                data["brand"]        = vision_data["marca_visivel"]
                data["brand_source"] = "vision"
                filled.append(f"brand='{data['brand']}'")
            if not data.get("model") and vision_data.get("modelo_visivel"):
                data["model"]        = vision_data["modelo_visivel"]
                data["model_source"] = "vision"
                filled.append(f"model='{data['model']}'")
            if not data.get("version") and vision_data.get("versao_visivel"):
                data["version"]        = vision_data["versao_visivel"]
                data["version_source"] = "vision"
                filled.append(f"version='{data['version']}'")
            if vision_data.get("detalhes_visiveis"):
                data["vision_technical_details"] = vision_data["detalhes_visiveis"]
            if filled:
                logger.info(f"Campos preenchidos via visão: {', '.join(filled)}")
            if not vision_data.get("condicao_consistente", True):
                logger.warning(
                    f"Condição inconsistente: vendedor declarou '{condition}' "
                    f"mas visão detectou: {vision_data.get('descricao_visual', '')[:100]}"
                )
                data["_condition_inconsistent"] = True

        # Recalcula product_name com campos enriquecidos pela visão
        brand        = data.get("brand") or ""
        model        = data.get("model") or ""
        version      = data.get("version") or ""
        product_name = " ".join(filter(None, [brand, model, version])) or description

        visual_desc = (vision_data or {}).get("descricao_visual", "") if vision_data else ""
        condition_ok = (vision_data or {}).get("condicao_consistente", True) if vision_data else True
        condition_alert = (
            f"ATENÇÃO: análise visual detecta inconsistência com a condição declarada. "
            f"Descrição visual: {visual_desc}. "
            if not condition_ok else ""
        )

        # 4. Precificação com todos os dados
        rich_description = (
            f"{product_name}. "
            f"Estado: {usage_state}. "
            f"Condição: {CONDITION_LABEL.get(condition, condition)}. "
            f"Nota fiscal: {'sim' if has_receipt else 'não'}. "
            + (f"Análise visual: {visual_desc}. " if visual_desc else "")
            + condition_alert
            + (f"Preços encontrados na web: {web_prices[:300]}." if web_prices else "")
        )
        pricing_agent = PricingAgent(db)
        appraisal = await pricing_agent.appraise(
            descricao=rich_description,
            categoria=category,
            preco_informado_vendedor=asking_price,
            historico_similares=similar_history,
            fotos=base64_images or None,
        )
        data["appraisal"] = appraisal

        data["seller_city"] = _extract_city(pickup_address)

        price_agent  = appraisal.get("preco_sugerido", 0) or 0
        min_agent    = appraisal.get("preco_minimo_sugerido", 0) or 0
        justification = appraisal.get("justificativa", "")
        confidence   = appraisal.get("confianca", "baixa")

        listed_price = asking_price or price_agent
        floor_price  = seller_min or min_agent

        data["listed_price"] = listed_price
        data["floor_price"]  = floor_price
        data["step_next"] = "review_condition" if data.get("_condition_inconsistent") else "confirm"

        price_alert = ""
        if asking_price and price_agent > 0:
            diff = abs(asking_price - price_agent) / price_agent
            if diff > 0.30:
                direction = "acima" if asking_price > price_agent else "abaixo"
                price_alert = (
                    f"Atenção: seu preço de R$ {asking_price:.2f} está "
                    f"{diff*100:.0f}% {direction} do valor de mercado de R$ {price_agent:.2f}. "
                )

        lines = [f"Produto: {product_name}"]

        origin_vision = [
            c for c in ("brand", "model", "version")
            if data.get(f"{c}_source") == "vision"
        ]
        if origin_vision:
            lines.append(f"  (detectado nas fotos: {', '.join(origin_vision)})")

        if vision_data and vision_data.get("detalhes_visiveis"):
            details = ", ".join(vision_data["detalhes_visiveis"][:4])
            lines.append(f"Detalhes lidos nas fotos: {details}")

        lines += [
            f"Estado: {usage_state} | Condição: {CONDITION_LABEL.get(condition, condition)}",
            f"Nota fiscal: {'sim' if has_receipt else 'não'}",
            f"Fotos: {len(photos)} enviada(s)",
            f"Retirada: {pickup_address or 'não informado'}",
        ]

        if asking_price:
            lines.append(f"Seu preço: R$ {asking_price:.2f}")
        if seller_min:
            lines.append(f"Seu mínimo: R$ {seller_min:.2f} (sigiloso)")
        lines.append(f"Avaliação NOTHA: R$ {price_agent:.2f} (confiança: {confidence})")
        lines.append(f"Motivo: {justification}")
        if price_alert:
            lines.append(price_alert)
        lines.append(f"Será anunciado por: R$ {listed_price:.2f}")

        summary = "\n".join(lines)

        if data.get("_condition_inconsistent"):
            declared_label = CONDITION_LABEL.get(condition, condition)
            visual_excerpt = visual_desc[:200] if visual_desc else "não disponível"
            msg = await self._reply(
                f"A análise das fotos detectou uma possível inconsistência na condição declarada.\n"
                f"Condição declarada: {declared_label}.\n"
                f"O que foi observado nas fotos: {visual_excerpt}\n\n"
                "Peça que o vendedor confirme se a condição está correta ou corrija para uma das opções:\n"
                "1. Como novo (sem marcas de uso)\n"
                "2. Bom estado (uso leve, poucas marcas)\n"
                "3. Conservado (uso normal, pequenos desgastes)\n"
                "4. Desgastado (uso intenso, marcas visíveis)\n"
                "5. Com defeito (descreva o defeito)\n"
                "Ou diga 'sim' para confirmar a condição já declarada."
            )
            return data, msg

        msg = await self._reply(
            f"Apresente o resumo do anúncio e pergunte se confirma:\n\n{summary}"
        )
        return data, msg

    async def _analyze_photos(
        self, photos: list, product: str, declared_condition: str,
        base64_images: list[str] | None = None,
    ) -> dict | None:
        """
        Usa GPT-4o Vision para analisar as fotos do produto.

        GUARDRAIL: só extrai texto que esteja LITERALMENTE IMPRESSO nas imagens.
        Aceita base64_images (data URIs já baixadas) para evitar download duplo.
        """
        from whatsapp import download_media_as_base64

        images = base64_images or []
        if not images:
            for photo in photos[:4]:
                data_uri = await download_media_as_base64(
                    photo.get("media_id", ""),
                    photo.get("mime_type", "image/jpeg"),
                )
                if data_uri:
                    images.append(data_uri)

        if not images:
            return None

        content: list = []
        for data_uri in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": data_uri, "detail": "high"},
            })

        content.append({
            "type": "text",
            "text": (
                f"Produto declarado: {product}.\n"
                f"Condição declarada pelo vendedor: {CONDITION_LABEL.get(declared_condition, declared_condition)}.\n\n"
                "Retorne SOMENTE um JSON válido com os campos abaixo. Nenhum texto fora do JSON.\n\n"
                "CAMPO 1 — descricao_visual (string):\n"
                "  Descreva objetivamente o estado físico visível: acabamento, arranhões, manchas, amassados, desgaste.\n"
                "  Se as fotos forem insuficientes (desfocadas, escuras), diga isso.\n\n"
                "CAMPO 2 — condicao_consistente (true | false):\n"
                "  A condição declarada é consistente com o que aparece nas fotos?\n\n"
                "CAMPO 3 — marca_visivel (string | null):\n"
                "  Leia a marca SOMENTE se ela estiver LITERALMENTE ESCRITA/IMPRESSA em etiqueta, caixa, tela ou adesivo.\n"
                "  NÃO infira a marca pela forma ou aparência do produto. Se não está escrito → null.\n\n"
                "CAMPO 4 — modelo_visivel (string | null):\n"
                "  Leia o modelo/nome do produto SOMENTE se LITERALMENTE ESCRITO nas imagens.\n"
                "  NÃO adivinhe pelo formato. Se não está escrito → null.\n\n"
                "CAMPO 5 — versao_visivel (string | null):\n"
                "  Leia versão/capacidade/variante SOMENTE se ESCRITA nas imagens.\n"
                "  NÃO infira pela cor ou tamanho. Se não está escrito → null.\n\n"
                "CAMPO 6 — detalhes_visiveis (array de strings):\n"
                "  Lista de quaisquer informações técnicas LIDAS literalmente nas imagens.\n"
                "  Inclua apenas o que está escrito. Array vazio [] se nada for legível.\n\n"
                "CAMPO 7 — fotos_suficientes (true | false):\n"
                "  As fotos têm qualidade e ângulos suficientes para avaliação confiável?\n\n"
                "REGRAS CRÍTICAS:\n"
                "- NUNCA atribua valor de mercado ou sugira preços.\n"
                "- NUNCA faça afirmações sobre autenticidade ou procedência.\n"
                "- Para marca/modelo/versao: se não está escrito na imagem, retorne null."
            ),
        })

        try:
            resp = await get_provider().complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é um avaliador visual de produtos físicos. "
                            "Retorne SEMPRE um JSON válido. "
                            "Só extraia informações literalmente visíveis nas imagens."
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                model="gpt-4o",
                max_tokens=600,
                temperature=0.0,
                json_mode=True,
            )
            raw = json.loads(resp.text or "{}")
            return {
                "descricao_visual":    str(raw.get("descricao_visual") or ""),
                "condicao_consistente": bool(raw.get("condicao_consistente", True)),
                "marca_visivel":        raw.get("marca_visivel") or None,
                "modelo_visivel":       raw.get("modelo_visivel") or None,
                "versao_visivel":       raw.get("versao_visivel") or None,
                "detalhes_visiveis":    list(raw.get("detalhes_visiveis") or []),
                "fotos_suficientes":    bool(raw.get("fotos_suficientes", True)),
            }
        except Exception as e:
            logger.warning(f"Análise visual falhou: {e}")
            return None


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _infer_category(name: str) -> str:
    n = name.lower()
    categories = {
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
    for cat, keywords in categories.items():
        if any(k in n for k in keywords):
            return cat
    return "outros"


def _extract_city(address: str) -> str | None:
    if not address:
        return None
    parts = [p.strip().rstrip(",") for p in address.split() if p.strip()]
    for i, part in enumerate(parts):
        if len(part) == 2 and part.isupper() and i > 0:
            return parts[i - 1]
    if len(parts) >= 2:
        return parts[-2]
    return None

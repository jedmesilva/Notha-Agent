"""
Orquestrador — roteamento central de mensagens.

O LLM recebe o histórico completo da conversa + ferramentas disponíveis e decide
sozinho quando chamar cada ferramenta. O código executa deterministicamente o que
o LLM decidiu. Princípio: LLM decide, código persiste.
"""
import logging
import re
from db.connection import DB, get_db
from db.repositories import (
    UserRepository, ListingRepository, ListingFlowRepository,
    NegotiationRepository, TransactionRepository, DeliveryRepository,
    ConversationRepository, BuscaSalvaRepository,
)
from agents.conversation import ConversationAgent, NOTHA_TOOLS
from agents.listing_flow import ListingFlowAgent, _parse_jsonb
from agents.pricing import PricingAgent
from agents.logistics import LogisticsAgent
from engine.negotiation import NegotiationEngine
from tools.builtin import web_search, currency, math, units, datetime_tool
from phone_timezone import infer_timezone

_BUILTIN_TOOL_MAP = {
    web_search.name: web_search,
    currency.name: currency,
    math.name: math,
    units.name: units,
    datetime_tool.name: datetime_tool,
}

logger = logging.getLogger("notha.orchestrator")

# Histórico de conversa persistido no banco via ConversationRepository.
# Este dict é apenas fallback para quando o banco não está disponível.
_MEMORY_HISTORY: dict[str, list[dict]] = {}
_MAX_MEMORY = 20

PENDING_CONFIRMATIONS: dict[str, dict] = {}
PROCESSED_MESSAGE_IDS: set[str] = set()
MAX_PROCESSED_IDS = 1000


def _parece_cpf(texto: str) -> bool:
    limpo = re.sub(r"[\.\-\s]", "", texto)
    return limpo.isdigit() and len(limpo) == 11


_PALAVRAS_INVALIDAS_NOME = {
    "oi", "olá", "ola", "opa", "ei", "hey",
    "sim", "não", "nao", "talvez", "ok", "okay",
    "tudo", "bem", "bom", "dia", "tarde", "noite",
    "boa", "boas", "certo", "claro", "pode", "vou",
    "quero", "queria", "preciso", "ajuda", "help",
    "alô", "alo", "eai", "eaí", "ae",
}


def _nome_valido(nome: str) -> bool:
    nome = nome.strip()
    if len(nome) < 2 or len(nome) > 60:
        return False
    if nome.isdigit():
        return False
    palavras = nome.lower().split()
    if not palavras:
        return False
    if all(p in _PALAVRAS_INVALIDAS_NOME for p in palavras):
        return False
    _PREP_NOMES = {"de", "da", "do", "das", "dos", "e"}
    palavras_significativas = [p for p in palavras if p not in _PREP_NOMES]
    if not palavras_significativas:
        return False
    if any(len(p) < 2 for p in palavras_significativas):
        return False
    return True


def _detectar_tipo_documento(caption: str) -> str:
    """Infere o tipo de documento pela legenda enviada com a imagem.

    Retorna: 'rg' | 'cnh' | 'passaporte' | 'desconhecido'
    """
    texto = caption.lower()
    if any(p in texto for p in ("rg", "identidade", "registro geral", "carteira de identidade")):
        return "rg"
    if any(p in texto for p in ("cnh", "habilitação", "habilitacao", "carteira de motorista")):
        return "cnh"
    if "passaporte" in texto:
        return "passaporte"
    return "desconhecido"


def _memory_add(phone: str, role: str, content: str) -> None:
    """Adiciona ao histórico em memória (fallback sem banco)."""
    hist = _MEMORY_HISTORY.setdefault(phone, [])
    hist.append({"role": role, "content": content})
    if len(hist) > _MAX_MEMORY:
        _MEMORY_HISTORY[phone] = hist[-_MAX_MEMORY:]


def _memory_get(phone: str) -> list[dict]:
    return _MEMORY_HISTORY.get(phone, [])


import asyncio as _asyncio

async def _gather(*coros):
    """Executa coroutines independentes em paralelo."""
    return await _asyncio.gather(*coros)


class Orchestrator:
    def __init__(self, db: DB | None = None):
        self._db = db
        self._conv = ConversationAgent()
        self._pricing = PricingAgent(db)
        self._listing_flow_agent = ListingFlowAgent()

    def _repos(self, db: DB):
        return (
            UserRepository(db),
            ListingRepository(db),
            NegotiationRepository(db),
            TransactionRepository(db),
            DeliveryRepository(db),
            ConversationRepository(db),
        )

    # Ferramentas que podem demorar e justificam uma mensagem de "aguarde"
    _SLOW_TOOLS = {"buscar_produto", "listar_produto", "pesquisar_web", "verificar_restricao"}

    # Fallback de mensagens de espera por ferramenta (quando o LLM não gera texto interim)
    _WAIT_MSG_FALLBACK = {
        "buscar_produto": "🔍 Pesquisando produtos disponíveis, um momento...",
        "listar_produto": "📝 Iniciando o cadastro do produto, um momento...",
        "pesquisar_web": "🌐 Consultando informações na web, um momento...",
        "verificar_restricao": "⏳ Verificando restrições, um momento...",
    }

    async def handle_message(self, phone: str, text: str, send_fn=None) -> str:
        """Processa a mensagem do usuário e retorna a resposta final.

        send_fn: coroutine opcional (phone, text) → None.
        Quando fornecida, mensagens intermediárias (ex: "aguarde, pesquisando...")
        são enviadas proativamente antes de ferramentas lentas, sem esperar o
        usuário perguntar de novo pelo resultado.
        """
        db = self._db or get_db()

        if db is None:
            return await self._no_db_fallback(phone, text)

        user_repo, listing_repo, neg_repo, tx_repo, delivery_repo, conv_repo = self._repos(db)
        engine = NegotiationEngine(db)
        flow_repo = ListingFlowRepository(db)

        user = await user_repo.find_or_create_by_phone(phone)
        user_id = user["id"]

        # Verifica se há um fluxo de cadastro de produto ativo para este telefone
        active_flow = await flow_repo.get_active(phone)
        if active_flow:
            return await self._handle_listing_flow_message(
                phone=phone,
                text=text,
                flow=dict(active_flow),
                user=user,
                user_repo=user_repo,
                listing_repo=listing_repo,
                flow_repo=flow_repo,
                conv_repo=conv_repo,
                db=db,
            )

        # Carrega dados em paralelo — necessário em todos os caminhos de resposta
        active_negs, seller_profile, history = await _gather(
            neg_repo.find_active_by_buyer(user_id),
            user_repo.get_seller_profile(user_id),
            conv_repo.get_history(user_id),
        )

        # Contexto rico com dados reais do banco — o LLM sempre trabalha com info atual
        contexto = self._build_context(user, active_negs, seller_profile, phone=phone)

        # Confirmações pendentes de negócio (ex: confirmar preço de anúncio)
        pending = PENDING_CONFIRMATIONS.get(phone)
        if pending:
            reply = await self._handle_confirmation(
                phone, text, pending, user, user_repo, listing_repo,
                neg_repo, tx_repo, delivery_repo, engine,
                history=history, contexto=contexto,
            )
            await conv_repo.add(user_id, "user", text)
            await conv_repo.add(user_id, "assistant", reply)
            return reply

        # Fase 1: LLM vê o histórico completo e decide quais ferramentas chamar
        messages, tool_calls = await self._conv.get_tool_calls(
            contexto=contexto,
            history=history,
            user_message=text,
            tools=NOTHA_TOOLS,
        )

        # Se há ferramentas lentas, envia mensagem de "aguarde" imediatamente
        # antes de executá-las — assim o usuário sabe que algo está acontecendo
        if send_fn and tool_calls:
            slow_tools = [tc for tc in tool_calls if tc["name"] in self._SLOW_TOOLS]
            if slow_tools:
                # Tenta usar o texto que o LLM gerou na fase 1 (mais natural)
                interim_text = next(
                    (m.get("content") for m in reversed(messages)
                     if m["role"] == "assistant" and m.get("content")),
                    None,
                )
                if not interim_text:
                    # Fallback: mensagem template baseada na primeira ferramenta lenta
                    interim_text = self._WAIT_MSG_FALLBACK.get(slow_tools[0]["name"])
                if interim_text:
                    try:
                        await send_fn(phone, interim_text)
                        logger.info("Mensagem intermediária enviada para %s: %s", phone, interim_text[:60])
                    except Exception as e:
                        logger.warning("Falha ao enviar mensagem intermediária: %s", e)

        # Ferramentas que alteram dados do usuário no banco — exigem recarregar contexto
        _USER_DATA_TOOLS = {
            "atualizar_nome", "atualizar_apelido", "atualizar_cpf",
            "atualizar_chave_pix", "atualizar_endereco", "atualizar_localizacao",
        }

        # Código executa deterministicamente o que o LLM decidiu
        # e devolve o resultado real do banco para o LLM gerar resposta precisa
        tool_results: dict[str, str] = {}
        override_reply: str | None = None
        user_data_changed = False

        for tc in tool_calls:
            if tc["name"] in _USER_DATA_TOOLS:
                user_data_changed = True
            result_text, complex_reply = await self._execute_tool(
                tc, phone, text, user,
                user_repo, listing_repo, neg_repo, engine, active_negs,
                history=history, contexto=contexto,
            )
            tool_results[tc["id"]] = result_text
            if complex_reply is not None:
                override_reply = complex_reply

        # Se alguma ferramenta atualizou dados do usuário, recarrega do banco
        # para que o contexto passado ao LLM na fase 2 reflita o estado real
        if user_data_changed:
            user = await user_repo.find_by_id(user_id) or user
            seller_profile = await user_repo.get_seller_profile(user_id)
            contexto = self._build_context(user, active_negs, seller_profile, phone=phone)

        if override_reply:
            final_reply = override_reply
        elif tool_calls:
            # Fase 2: LLM recebe os resultados reais e gera resposta natural
            final_reply = await self._conv.get_reply_after_tools(messages, tool_results, contexto=contexto)
        else:
            last_assistant = next(
                (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
                None,
            )
            if last_assistant:
                final_reply = last_assistant
            else:
                final_reply, _ = await self._conv.chat_with_tools(
                    contexto=contexto,
                    history=history,
                    user_message=text,
                    tools=None,
                )

            # Se há negociação ativa, verifica se é confirmação/recusa
            if active_negs:
                neg_reply = await self._check_negotiation_response(
                    phone, text, user, active_negs[0],
                    user_repo, neg_repo, listing_repo, engine,
                    history=history,
                )
                if neg_reply:
                    final_reply = neg_reply

        # Persiste mensagem e resposta no banco
        await conv_repo.add(user_id, "user", text)
        await conv_repo.add(user_id, "assistant", final_reply)
        return final_reply

    async def _execute_tool(
        self, tc: dict, phone: str, text: str, user,
        user_repo: UserRepository, listing_repo: ListingRepository,
        neg_repo: NegotiationRepository, engine: NegotiationEngine,
        active_negs: list,
        history: list[dict] | None = None,
        contexto: str = "",
    ) -> tuple[str, str | None]:
        """Executa deterministicamente a ferramenta que o LLM escolheu.

        Retorna (result_text, complex_reply):
        - result_text: resultado real do banco, passado de volta ao LLM para gerar resposta precisa
        - complex_reply: str se o fluxo produz seu próprio reply (listar, buscar), None caso contrário
        """
        name = tc["name"]
        args = tc["arguments"]

        if name == "atualizar_nome":
            nome = args.get("nome", "").strip()
            if _nome_valido(nome):
                await user_repo.update(user["id"], nome=nome)
                user_atualizado = await user_repo.find_by_id(user["id"])
                nome_salvo = user_atualizado.get("nome") if user_atualizado else nome
                apelido_salvo = user_atualizado.get("apelido") if user_atualizado else ""
                cpf_ok = bool(user_atualizado.get("cpf")) if user_atualizado else False
                logger.info("Nome atualizado via tool: '%s' (user_id=%s)", nome_salvo, user["id"])
                result = (
                    f"Nome legal salvo no banco: '{nome_salvo}'. "
                    f"Apelido: '{apelido_salvo or 'não definido'}'. "
                    f"CPF: {'registrado' if cpf_ok else 'ainda não registrado'}."
                )
            else:
                logger.warning("Nome rejeitado pela validação: '%s'", nome)
                result = (
                    f"Nome '{nome}' rejeitado (parece saudação ou inválido). "
                    f"Nome atual no banco: '{user.get('nome') or 'vazio'}'."
                )
            return result, None

        if name == "atualizar_apelido":
            apelido = args.get("apelido", "").strip()
            if apelido and len(apelido) >= 2:
                await user_repo.update_apelido(user["id"], apelido)
                logger.info("Apelido atualizado via tool: '%s' (user_id=%s)", apelido, user["id"])
                result = (
                    f"Apelido salvo no banco: '{apelido}'. "
                    f"O usuário agora será chamado de '{apelido}'. "
                    f"Nome legal permanece: '{user.get('nome') or 'não informado'}'."
                )
            else:
                result = f"Apelido vazio ou muito curto — nenhuma alteração feita."
            return result, None

        if name == "atualizar_cpf":
            cpf_bruto = args.get("cpf", "").strip()
            cpf = re.sub(r"[\.\-\s]", "", cpf_bruto)
            if _parece_cpf(cpf):
                existing = await user_repo.find_by_cpf(cpf)
                if existing and existing["id"] != user["id"]:
                    await user_repo.add_phone(existing["id"], phone)
                    logger.info("CPF já existia — telefone transferido para user_id=%s", existing["id"])
                    result = f"CPF já estava cadastrado para '{existing.get('nome') or 'usuário'}'. Histórico recuperado."
                else:
                    await user_repo.update(user["id"], cpf=cpf)
                    logger.info("CPF atualizado via tool (user_id=%s)", user["id"])
                    result = f"CPF '{cpf}' salvo no banco para user_id={user['id']}. Nome: '{user.get('nome') or 'vazio'}'."
            else:
                logger.warning("CPF inválido recebido: '%s'", cpf_bruto)
                result = f"CPF '{cpf_bruto}' inválido (precisa ter 11 dígitos). CPF atual no banco: {'registrado' if user.get('cpf') else 'vazio'}."
            return result, None

        if name == "atualizar_chave_pix":
            chave = args.get("chave", "").strip()
            if chave:
                await user_repo.upsert_seller_profile(user["id"], chave_pix=chave)
                logger.info("Chave Pix atualizada (user_id=%s)", user["id"])
                result = f"Chave Pix '{chave}' salva no banco para user_id={user['id']}."
            else:
                result = "Chave Pix vazia — nenhuma alteração feita."
            return result, None

        if name == "atualizar_endereco":
            endereco = args.get("endereco", "").strip()
            if endereco:
                await user_repo.upsert_seller_profile(user["id"], endereco_retirada=endereco)
                logger.info("Endereço atualizado (user_id=%s)", user["id"])
                result = f"Endereço de retirada '{endereco}' salvo no banco para user_id={user['id']}."
            else:
                result = "Endereço vazio — nenhuma alteração feita."
            return result, None

        if name == "atualizar_localizacao":
            cidade = args.get("cidade", "").strip() or None
            bairro = args.get("bairro", "").strip() or None
            if cidade or bairro:
                await user_repo.update_localizacao(user["id"], cidade=cidade, bairro=bairro)
                logger.info("Localização atualizada (user_id=%s): cidade=%s bairro=%s", user["id"], cidade, bairro)
                parts = []
                if cidade:
                    parts.append(f"cidade='{cidade}'")
                if bairro:
                    parts.append(f"bairro='{bairro}'")
                result = f"Localização salva no banco: {', '.join(parts)}. Usar para filtrar buscas de produtos."
            else:
                result = "Nenhuma localização informada — nenhuma alteração feita."
            return result, None

        if name == "salvar_interesse":
            descricao = args.get("descricao_busca", "").strip()
            if not descricao:
                return "Descrição de busca vazia — interesse não salvo.", None
            db = self._db or get_db()
            if db:
                busca_repo = BuscaSalvaRepository(db)
                busca = await busca_repo.criar(
                    user_id=user["id"],
                    phone=phone,
                    descricao_busca=descricao,
                    categoria=args.get("categoria", "").strip() or None,
                    cidade_busca=args.get("cidade_busca", "").strip() or None,
                    bairro_busca=args.get("bairro_busca", "").strip() or None,
                )
                logger.info("Interesse salvo (user_id=%s): '%s' id=%s", user["id"], descricao, busca["id"])
                result = f"Alerta de interesse salvo (id={busca['id']}): '{descricao}'. Usuário será notificado via WhatsApp assim que aparecer um produto compatível."
            else:
                result = "Banco indisponível — interesse não salvo."
            return result, None

        if name == "cancelar_alertas":
            db = self._db or get_db()
            if db:
                busca_repo = BuscaSalvaRepository(db)
                alertas = await busca_repo.listar_por_user(user["id"])
                await busca_repo.cancelar_todas_do_user(user["id"])
                logger.info("Alertas cancelados (user_id=%s): %d alertas", user["id"], len(alertas))
                result = f"{len(alertas)} alerta(s) de busca cancelado(s) para user_id={user['id']}."
            else:
                result = "Banco indisponível — alertas não cancelados."
            return result, None

        if name == "listar_produto":
            db = self._db or get_db()
            complex_reply = await self._start_listing_flow(
                phone=phone,
                text=text,
                user=user,
                user_repo=user_repo,
                db=db,
                history=history or [],
                contexto=contexto,
            )
            return "fluxo de cadastro iniciado", complex_reply

        if name == "buscar_produto":
            intent = {
                "categoria": args.get("categoria"),
                "descricao_busca": args.get("descricao_busca"),
                "cidade_busca": args.get("cidade_busca", "").strip() or None,
                "bairro_busca": args.get("bairro_busca", "").strip() or None,
            }
            complex_reply = await self._handle_search(
                phone, text, user, listing_repo, intent,
                history=history or [], contexto=contexto,
            )
            return "busca executada", complex_reply

        if name in _BUILTIN_TOOL_MAP:
            result = await _BUILTIN_TOOL_MAP[name].execute(**args)
            logger.info("Tool builtin '%s' executada com sucesso", name)
            return result, None

        logger.warning("Ferramenta desconhecida chamada pelo LLM: %s", name)
        return f"ferramenta '{name}' desconhecida", None

    async def _check_negotiation_response(
        self, phone: str, text: str, user, neg,
        user_repo, neg_repo, listing_repo, engine,
        history: list[dict] | None = None,
    ) -> str | None:
        """Verifica se a mensagem é uma confirmação/recusa de negociação ativa."""
        intent = await self._conv.extract_intent(text, contexto="negociacao_ativa")
        intencao = intent.get("intencao", "outro")
        if intencao in ("confirmacao", "recusa"):
            return await self._handle_negotiation_response(
                phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine,
                history=history or [],
            )
        return None

    async def _no_db_fallback(self, phone: str, text: str) -> str:
        messages, _ = await self._conv.get_tool_calls(
            contexto="sem banco de dados disponível — modo memória apenas",
            history=_memory_get(phone),
            user_message=text,
            tools=NOTHA_TOOLS,
        )
        last_assistant = next(
            (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
            "Tive um problema técnico. Tente de novo em instantes!",
        )
        _memory_add(phone, "user", text)
        _memory_add(phone, "assistant", last_assistant)
        return last_assistant

    def _build_context(self, user, active_negs: list, seller_profile=None, phone: str = "") -> str:
        """Monta o contexto com dados reais do banco para o LLM.

        Inclui nome, apelido, CPF, verificação de identidade, perfil de vendedor
        e negociações ativas — tudo direto do banco, nada inventado.
        """
        parts = []

        nome = user.get("nome") or ""
        apelido = user.get("apelido") or ""
        cpf = user.get("cpf") or ""
        user_id = user.get("id", "?")
        status_identidade = user.get("status_identidade") or "nao_verificado"

        # Nome legal
        if not nome:
            parts.append("STATUS: usuário sem nome cadastrado — peça o nome completo")
        elif not _nome_valido(nome):
            parts.append(
                f"STATUS: nome='{nome}' parece incorreto — "
                "capture o nome real se o usuário mencionar"
            )
        else:
            parts.append(f"nome: {nome}")

        # Apelido (como chamar o usuário)
        if apelido:
            parts.append(f"apelido: {apelido}")
        else:
            parts.append("apelido: não definido")

        # CPF e verificação de identidade
        parts.append(f"CPF: {'registrado (✓)' if cpf else 'não registrado'}")

        _IDENTIDADE_LABEL = {
            "nao_verificado": "não verificado",
            "em_analise": "em análise (documento enviado)",
            "verificado": "verificado (✓)",
            "rejeitado": "rejeitado (documento inválido — peça novo envio)",
        }
        parts.append(f"identidade: {_IDENTIDADE_LABEL.get(status_identidade, status_identidade)}")
        parts.append(f"user_id: {user_id}")

        # Endereço de moradia do usuário (cidade/bairro onde ele vive — para entregas)
        cidade_mora = user.get("cidade") or ""
        bairro_mora = user.get("bairro") or ""
        if cidade_mora or bairro_mora:
            loc_parts = []
            if bairro_mora:
                loc_parts.append(f"bairro={bairro_mora}")
            if cidade_mora:
                loc_parts.append(f"cidade={cidade_mora}")
            parts.append(f"mora em: {', '.join(loc_parts)} (endereço de moradia — para entregas; região de BUSCA é perguntada na hora)")
        else:
            parts.append("mora em: não informado (se quiser usar como referência de busca, pergunte cidade/bairro de moradia)")

        # Perfil de vendedor
        if seller_profile:
            chave_pix = seller_profile.get("chave_pix") or ""
            endereco = seller_profile.get("endereco_retirada") or ""
            parts.append(f"chave Pix: {chave_pix if chave_pix else 'não cadastrada'}")
            if endereco:
                parts.append(f"endereço retirada: {endereco}")
        else:
            parts.append("perfil vendedor: não criado")

        # Negociações ativas
        if active_negs:
            neg = active_negs[0]
            parts.append(
                f"negociação ativa: id={neg['id']}, status={neg['status']}, "
                f"valor=R${neg.get('preco_atual_proposto', 0):.2f}"
            )
        else:
            parts.append("sem negociações ativas")

        # Fuso horário inferido: cidade cadastrada > DDI+código de área > DDI
        cidade_para_tz = cidade_mora or ""
        tz = infer_timezone(phone, cidade=cidade_para_tz)
        parts.append(f"fuso_horario: {tz}")

        return " | ".join(parts)

    async def _handle_list_product(self, phone, text, user, user_repo, listing_repo, intent,
                                   history: list[dict] | None = None, contexto: str = "") -> str:
        check = await user_repo.check_missing_fields(user["id"], "listar_produto")
        if check["falta"]:
            campos = ", ".join(check["falta"])
            return await self._conv.speak(
                f"Para listar um produto, preciso de: {campos}. Peça de forma natural.",
                history, contexto,
            )

        descricao = intent.get("descricao", text)
        categoria = intent.get("categoria")
        preco_informado = intent.get("preco_informado")

        historico_similares = []
        if categoria:
            rows = await listing_repo.find_similar_sold(categoria)
            historico_similares = [dict(r) for r in rows]

        appraisal = await self._pricing.appraise_with_web_search(
            descricao=descricao,
            categoria=categoria,
            preco_informado_vendedor=preco_informado,
            historico_similares=historico_similares,
        )

        PENDING_CONFIRMATIONS[phone] = {
            "tipo": "confirmar_preco_listing",
            "appraisal": appraisal,
            "descricao": descricao,
            "categoria": categoria,
            "preco_informado": preco_informado,
            "seller_id": user["id"],
        }

        alerta = ""
        if appraisal.get("alerta_preco_vendedor"):
            alerta = " (Atenção: o preço que você informou difere muito do mercado!)"

        return await self._conv.speak(
            f"Avaliei o produto. Preço sugerido: R${appraisal['preco_sugerido']:.2f}{alerta}. "
            f"Justificativa: {appraisal['justificativa']}. "
            f"Comunique o preço sugerido e pergunte se confirma o anúncio por esse valor "
            f"(mínimo interno: R${appraisal['preco_minimo_sugerido']:.2f}). Termine com pergunta de confirmação sim/não.",
            history, contexto,
        )

    async def _handle_search(self, phone, text, user, listing_repo, intent,
                             history: list[dict] | None = None, contexto: str = "") -> str:
        historia = history or []
        categoria = intent.get("categoria")
        descricao_busca = intent.get("descricao_busca") or categoria or "produto"
        cidade_busca: str | None = intent.get("cidade_busca")
        bairro_busca: str | None = intent.get("bairro_busca")

        # --- Nível 1: busca com filtro completo (bairro + cidade) ---
        listings = await listing_repo.find_available(
            categoria=categoria, limit=5,
            cidade=cidade_busca, bairro=bairro_busca,
        )
        if listings:
            regiao_label = (
                f"no bairro {bairro_busca}" if bairro_busca else
                f"em {cidade_busca}" if cidade_busca else
                "disponíveis"
            )
            return await self._format_search_results(listings, regiao_label, historia, contexto)

        # --- Nível 2: se buscou por bairro e não achou, tenta só a cidade ---
        if bairro_busca and cidade_busca:
            listings = await listing_repo.find_available(
                categoria=categoria, limit=5, cidade=cidade_busca,
            )
            if listings:
                prefixo = f"Nada no {bairro_busca}, mas achei em {cidade_busca}:"
                return await self._format_search_results(listings, f"em {cidade_busca}", historia, contexto, prefixo=prefixo)

        # --- Nível 3: tenta Brasil inteiro ---
        if cidade_busca or bairro_busca:
            listings = await listing_repo.find_available(categoria=categoria, limit=5)
            regiao_original = bairro_busca or cidade_busca or "essa região"
            if listings:
                prefixo = f"Não encontrei nada em {regiao_original}. Mas tem isso disponível em outras regiões:"
                return await self._format_search_results(listings, "em outras regiões", historia, contexto, prefixo=prefixo)

        # --- Nada em lugar nenhum ---
        regiao_original = bairro_busca or cidade_busca or "qualquer região"
        return await self._conv.speak(
            f"Nenhum '{descricao_busca}' disponível agora em {regiao_original}. "
            f"Informe isso e pergunte se quer salvar um alerta para ser avisado quando aparecer.",
            historia, contexto,
        )

    async def _notify_interested_users(self, listing: dict, db: DB) -> None:
        """Verifica buscas salvas ativas e notifica via WhatsApp quem tem interesse no listing."""
        try:
            from whatsapp import send_message as _wpp_send
            busca_repo = BuscaSalvaRepository(db)
            buscas = await busca_repo.listar_ativas()
            for busca in buscas:
                if not busca_repo.matches(busca, listing):
                    continue
                nome_produto = listing.get("descricao") or "Produto"
                cidade = listing.get("cidade_vendedor") or ""
                preco = listing.get("preco_anunciado") or 0
                loc_txt = f" em {cidade}" if cidade else ""
                msg = (
                    f"Achei um produto que pode te interessar{loc_txt}!\n\n"
                    f"📦 {nome_produto}\n"
                    f"💰 R${preco:.2f}\n\n"
                    f"Quer ver mais detalhes ou negociar? É só responder aqui!"
                )
                try:
                    await _wpp_send(busca["phone"], msg)
                    await busca_repo.registrar_notificacao(busca["id"])
                    logger.info(
                        "Notificação enviada: busca_id=%s phone=%s listing_id=%s",
                        busca["id"], busca["phone"], listing.get("id"),
                    )
                except Exception as e:
                    logger.warning("Falha ao notificar busca_id=%s: %s", busca["id"], e)
        except Exception as e:
            logger.error("Erro em _notify_interested_users: %s", e)

    async def _format_search_results(
        self, listings: list, regiao_label: str,
        history: list[dict] | None = None, contexto: str = "", prefixo: str = ""
    ) -> str:
        items = [
            f"• {l['descricao']} — R${l['preco_anunciado']:.2f}"
            f" ({l.get('cidade_vendedor') or 'localização não informada'})"
            for l in listings
        ]
        corpo = "\n".join(items)
        instrucao = (
            f"{prefixo}\n{corpo}".strip() if prefixo
            else f"Encontrei {len(listings)} produto(s) {regiao_label}:\n{corpo}"
        )
        instrucao += "\n\nPergunte se o usuário quer negociar algum."
        return await self._conv.speak(instrucao, history or [], contexto)

    async def _handle_negotiation_response(
        self, phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine,
        history: list[dict] | None = None,
        contexto: str = "",
    ) -> str:
        hist = history or []
        aceitou = intent.get("aceitou", False)
        status = neg["status"]

        if status == "proposta_ao_vendedor":
            if aceitou:
                await engine.aceitar_proposta_vendedor(neg["id"])
                return await self._conv.speak(
                    f"Proposta de R${neg['preco_atual_proposto']:.2f} confirmada. Comunique de forma positiva e informe que o comprador será notificado.",
                    hist, contexto,
                )
            else:
                await engine.recusar_proposta_vendedor(neg["id"])
                return await self._conv.speak(
                    "Proposta recusada. Informe que vai renegociar com o comprador e trazer uma nova proposta.",
                    hist, contexto,
                )

        if status == "proposta_ao_comprador":
            if aceitou:
                await engine.aceitar_proposta_comprador(neg["id"])
                return await self._conv.speak(
                    f"Negócio fechado em R${neg['preco_atual_proposto']:.2f}! Comunique o fechamento e informe que o link de pagamento será gerado.",
                    hist, contexto,
                )
            else:
                await engine.recusar_proposta_comprador(neg["id"])
                return await self._conv.speak(
                    "Proposta recusada pelo comprador. Informe que vai tentar uma nova rodada de negociação.",
                    hist, contexto,
                )

        reply, _ = await self._conv.chat_with_tools(
            contexto=contexto or f"negociação status={status}",
            history=hist,
            user_message=intent.get("descricao", ""),
            tools=None,
        )
        return reply

    async def _handle_confirmation(
        self, phone, text, pending, user, user_repo, listing_repo,
        neg_repo, tx_repo, delivery_repo, engine,
        history: list[dict] | None = None, contexto: str = "",
    ) -> str:
        hist = history or []
        tipo = pending.get("tipo")
        intent = await self._conv.extract_intent(text, contexto="confirmacao")
        aceitou = intent.get("aceitou", False)

        if tipo == "confirmar_preco_listing":
            PENDING_CONFIRMATIONS.pop(phone, None)
            if not aceitou:
                return await self._conv.speak(
                    "Usuário não confirmou o preço. Peça de forma natural que informe o preço que prefere anunciar.",
                    hist, contexto,
                )
            appraisal = pending["appraisal"]
            listing = await listing_repo.create(
                seller_id=pending["seller_id"],
                descricao=pending["descricao"],
                categoria=pending.get("categoria"),
                preco_informado_vendedor=pending.get("preco_informado"),
                preco_sugerido=appraisal["preco_sugerido"],
                preco_anunciado=appraisal["preco_sugerido"],
                preco_minimo=appraisal["preco_minimo_sugerido"],
                appraisal_data=appraisal,
            )
            db = self._db or get_db()
            if db:
                import asyncio as _asyncio
                _asyncio.create_task(self._notify_interested_users(dict(listing), db))
            return await self._conv.speak(
                f"Produto anunciado com sucesso (ID #{listing['id']}, "
                f"R${appraisal['preco_sugerido']:.2f}). "
                "Comunique a confirmação de forma positiva e informe que avisará quando houver interessados.",
                hist, contexto,
            )

        PENDING_CONFIRMATIONS.pop(phone, None)
        return await self._conv.speak("Ação cancelada. Informe de forma natural.", hist, contexto)

    # ─────────────────────────────────────────────
    # Fluxo de cadastro de produto (listing flow)
    # ─────────────────────────────────────────────

    async def _start_listing_flow(
        self, phone: str, text: str, user, user_repo: UserRepository, db: DB,
        history: list[dict] | None = None, contexto: str = "",
    ) -> str:
        """Inicia o fluxo de cadastro de produto (state machine persistida no banco)."""
        check = await user_repo.check_missing_fields(user["id"], "listar_produto")
        if check["falta"]:
            campos = ", ".join(check["falta"])
            return await self._conv.speak(
                f"Para listar um produto, preciso de: {campos}. Peça de forma natural.",
                history or [], contexto,
            )

        flow_repo = ListingFlowRepository(db)

        # Cancela eventual fluxo preso (step != concluido)
        await flow_repo.cancel(phone)

        # Cria novo fluxo
        flow = await flow_repo.create(user["id"], phone)

        primeira_pergunta = await self._listing_flow_agent.start()
        return primeira_pergunta

    async def _handle_listing_flow_message(
        self,
        phone: str,
        text: str,
        flow: dict,
        user,
        user_repo: UserRepository,
        listing_repo: ListingRepository,
        flow_repo: ListingFlowRepository,
        conv_repo: ConversationRepository,
        db: DB,
    ) -> str:
        """Roteia a mensagem de texto para o agente de fluxo de cadastro."""
        seller_profile = await user_repo.get_seller_profile(user["id"])
        sp = dict(seller_profile) if seller_profile else {}

        dados, fotos, reply, concluido = await self._listing_flow_agent.handle_message(
            flow=flow,
            text=text,
            seller_profile=sp,
            db=db,
        )

        next_step = dados.get("step_next", flow["step"])
        await flow_repo.update_step(flow["id"], next_step, dados, fotos)

        # Persiste histórico
        await conv_repo.add(user["id"], "user", text)

        # Etapa de processamento automático: envia "aguarde" → processa → retorna confirmação
        if next_step == "processando":
            if reply:
                from whatsapp import send_message as _wpp_send
                await _wpp_send(phone, reply)

            flow_atualizado = await flow_repo.get_active(phone)
            if flow_atualizado:
                dados_proc, msg_confirmar = await self._listing_flow_agent.processar(
                    flow=dict(flow_atualizado),
                    listing_repo=listing_repo,
                    db=db,
                )
                fotos_atuais = _parse_jsonb(flow_atualizado.get("fotos"), [])
                # step_next pode ser "confirmar" (normal) ou "revisar_condicao" (inconsistência visual)
                proximo_step = dados_proc.get("step_next", "confirmar")
                await flow_repo.update_step(flow_atualizado["id"], proximo_step, dados_proc, fotos_atuais)
                await conv_repo.add(user["id"], "assistant", msg_confirmar)
                return msg_confirmar
            return reply or "Processando seu produto..."

        # Fluxo confirmado: cria listing no banco
        if concluido:
            result_msg = await self._finalize_listing(
                flow_id=flow["id"],
                dados=dados,
                fotos=fotos,
                user=user,
                listing_repo=listing_repo,
                flow_repo=flow_repo,
            )
            await conv_repo.add(user["id"], "assistant", result_msg)
            return result_msg

        if reply:
            await conv_repo.add(user["id"], "assistant", reply)
        return reply or "Ok! Pode continuar."

    async def _finalize_listing(
        self,
        flow_id: int,
        dados: dict,
        fotos: list,
        user,
        listing_repo: ListingRepository,
        flow_repo: ListingFlowRepository,
    ) -> str:
        """Cria o listing no banco com todos os dados coletados e marca o fluxo como concluído."""
        appraisal = dados.get("appraisal", {})
        nome_produto = " ".join(
            filter(None, [dados.get("marca"), dados.get("modelo"), dados.get("versao")])
        ) or dados.get("descricao", "Produto")

        listing = await listing_repo.create(
            seller_id=user["id"],
            descricao=dados.get("descricao", nome_produto),
            categoria=dados.get("categoria"),
            fotos=[f["media_id"] for f in fotos if f.get("media_id")],
            preco_informado_vendedor=dados.get("preco_desejado"),
            preco_sugerido=appraisal.get("preco_sugerido"),
            preco_anunciado=dados.get("preco_anunciado") or appraisal.get("preco_sugerido", 0),
            preco_minimo=dados.get("preco_minimo") or appraisal.get("preco_minimo_sugerido", 0),
            appraisal_data=appraisal,
            marca=dados.get("marca"),
            modelo=dados.get("modelo"),
            versao=dados.get("versao"),
            estado_uso=dados.get("estado_uso"),
            condicao=dados.get("condicao"),
            tem_nota_fiscal=dados.get("tem_nota_fiscal"),
            preco_minimo_vendedor=dados.get("preco_minimo_vendedor"),
            info_web=dados.get("info_web"),
            cidade_vendedor=dados.get("cidade_vendedor"),
            vision_analysis=dados.get("vision_analysis"),
        )

        await flow_repo.mark_done(flow_id)

        db = self._db or get_db()
        if db:
            import asyncio as _asyncio
            _asyncio.create_task(self._notify_interested_users(dict(listing), db))

        preco = dados.get("preco_anunciado") or appraisal.get("preco_sugerido", 0) or 0
        return (
            f"Produto anunciado com sucesso! ID #{listing['id']}.\n"
            f"Nome: {nome_produto}\n"
            f"Preço: R${preco:.2f}\n"
            "Vou te avisar assim que aparecer um interessado!"
        )

    async def handle_media(
        self,
        phone: str,
        media_id: str,
        mime_type: str,
        caption: str,
    ) -> str:
        """
        Roteador central de mídia recebida.

        Prioridade:
        1. Se há listing flow ativo na etapa 'fotos' → rota para o agente de cadastro
        2. Caso contrário → identifica como documento de identidade
        """
        db = self._db or get_db()
        if db is None:
            return "Recebi sua imagem! Mas estou com problema técnico — tenta de novo em instantes."

        user_repo, listing_repo, *_, conv_repo = self._repos(db)
        flow_repo = ListingFlowRepository(db)

        user = await user_repo.find_or_create_by_phone(phone)

        active_flow = await flow_repo.get_active(phone)
        if active_flow and active_flow["step"] == "fotos":
            fotos_atuais, reply = await self._listing_flow_agent.handle_media(
                flow=dict(active_flow),
                media_id=media_id,
                mime_type=mime_type,
                caption=caption or "",
            )
            dados_atuais = _parse_jsonb(active_flow.get("dados"), {})
            await flow_repo.update_step(active_flow["id"], "fotos", dados_atuais, fotos_atuais)
            if reply:
                await conv_repo.add(user["id"], "assistant", reply)
            return reply

        # Fallback: trata como documento de identidade
        return await self.handle_identity_document(phone, media_id, mime_type, caption)

    async def handle_identity_document(
        self,
        phone: str,
        media_id: str,
        mime_type: str,
        caption: str,
    ) -> str:
        """Processa imagem/documento enviado pelo usuário como documento de identidade.

        Fluxo:
        1. Busca o usuário no banco (cria se for o primeiro contato)
        2. Detecta o tipo de documento pela caption (rg, cnh, passaporte)
        3. Baixa a imagem do WhatsApp e faz upload para Supabase Storage
        4. Registra no banco e atualiza status_identidade para 'em_analise'
        5. Retorna mensagem natural ao usuário
        """
        from storage.identity import processar_documento_identidade

        db = self._db or get_db()
        if db is None:
            return "Recebi seu documento! Mas estou com problema técnico no momento — tenta de novo em instantes."

        user_repo, *_ = self._repos(db)
        user = await user_repo.find_or_create_by_phone(phone)
        user_id = user["id"]
        nome_display = user.get("apelido") or (user.get("nome") or "").split()[0] or ""

        # Detecta tipo do documento pela legenda enviada com a imagem
        tipo = _detectar_tipo_documento(caption or "")

        try:
            resultado = await processar_documento_identidade(
                user_id=user_id,
                media_id=media_id,
                tipo=tipo,
                user_repo=user_repo,
            )
            logger.info(
                "Documento de identidade salvo: user_id=%s tipo=%s doc_id=%s path=%s",
                user_id, tipo, resultado.get("doc_id"), resultado.get("object_path"),
            )
        except Exception as e:
            logger.error("Falha ao processar documento de identidade (user_id=%s): %s", user_id, e)
            return (
                "Recebi a imagem, mas houve um problema técnico ao salvá-la. "
                "Pode enviar de novo? Se persistir, tente em formato JPG ou PNG."
            )

        prefixo = f"{nome_display}, " if nome_display else ""
        tipo_label = {
            "rg": "RG",
            "cnh": "CNH",
            "passaporte": "passaporte",
        }.get(tipo, "documento")

        return (
            f"{prefixo}recebi seu {tipo_label}! "
            "Vou analisar e te aviso assim que a verificação for concluída. "
            "Normalmente leva até 1 dia útil."
        )

    async def reset(self, phone: str) -> None:
        """Apaga histórico do banco e memória; remove confirmações pendentes."""
        db = self._db or get_db()
        PENDING_CONFIRMATIONS.pop(phone, None)
        _MEMORY_HISTORY.pop(phone, None)
        if db is None:
            return
        user_repo, *_, conv_repo = self._repos(db)
        user = await user_repo.find_by_phone(phone)
        if user:
            await conv_repo.clear(user["id"])
            logger.info("Histórico apagado para user_id=%s", user["id"])

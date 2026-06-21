"""
Orchestrator — central message routing.

The LLM receives the full conversation history + available tools and decides
on its own when to call each tool. The code deterministically executes what
the LLM decided. Principle: LLM decides, code persists.
"""
import logging
import re
from db.connection import DB, get_db
from db.repositories import (
    UserRepository, ListingRepository, ListingFlowRepository,
    NegotiationRepository, TransactionRepository, DeliveryRepository,
    ConversationRepository, SavedSearchRepository,
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
    currency.name:   currency,
    math.name:       math,
    units.name:      units,
    datetime_tool.name: datetime_tool,
}

logger = logging.getLogger("notha.orchestrator")

# Conversation history persisted in DB via ConversationRepository.
# This dict is fallback only when the DB is unavailable.
_MEMORY_HISTORY: dict[str, list[dict]] = {}
_MAX_MEMORY = 20

PENDING_CONFIRMATIONS: dict[str, dict] = {}
PROCESSED_MESSAGE_IDS: set[str] = set()
MAX_PROCESSED_IDS = 1000


def _looks_like_cpf(text: str) -> bool:
    cleaned = re.sub(r"[\.\-\s]", "", text)
    return cleaned.isdigit() and len(cleaned) == 11


_INVALID_NAME_WORDS = {
    "oi", "olá", "ola", "opa", "ei", "hey",
    "sim", "não", "nao", "talvez", "ok", "okay",
    "tudo", "bem", "bom", "dia", "tarde", "noite",
    "boa", "boas", "certo", "claro", "pode", "vou",
    "quero", "queria", "preciso", "ajuda", "help",
    "alô", "alo", "eai", "eaí", "ae",
}


def _is_valid_name(name: str) -> bool:
    name = name.strip()
    if len(name) < 2 or len(name) > 60:
        return False
    if name.isdigit():
        return False
    words = name.lower().split()
    if not words:
        return False
    if all(w in _INVALID_NAME_WORDS for w in words):
        return False
    _NAME_PREPOSITIONS = {"de", "da", "do", "das", "dos", "e"}
    significant_words = [w for w in words if w not in _NAME_PREPOSITIONS]
    if not significant_words:
        return False
    if any(len(w) < 2 for w in significant_words):
        return False
    return True


def _detect_document_type(caption: str) -> str:
    """Infers document type from the caption sent with the image.

    Returns: 'national_id' | 'drivers_license' | 'passport' | 'unknown'
    """
    text = caption.lower()
    if any(p in text for p in ("rg", "identidade", "registro geral", "carteira de identidade")):
        return "national_id"
    if any(p in text for p in ("cnh", "habilitação", "habilitacao", "carteira de motorista")):
        return "drivers_license"
    if "passaporte" in text:
        return "passport"
    return "unknown"


def _memory_add(phone: str, role: str, content: str) -> None:
    """Adds to in-memory history (DB-unavailable fallback)."""
    hist = _MEMORY_HISTORY.setdefault(phone, [])
    hist.append({"role": role, "content": content})
    if len(hist) > _MAX_MEMORY:
        _MEMORY_HISTORY[phone] = hist[-_MAX_MEMORY:]


def _memory_get(phone: str) -> list[dict]:
    return _MEMORY_HISTORY.get(phone, [])


import asyncio as _asyncio

async def _gather(*coros):
    """Runs independent coroutines in parallel."""
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

    # Tools that may take a while and justify a "please wait" message
    _SLOW_TOOLS = {"search_product", "list_product", "pesquisar_web", "verificar_restricao"}

    # Wait message fallback by tool (when the LLM doesn't generate interim text)
    _WAIT_MSG_FALLBACK = {
        "search_product":    "🔍 Pesquisando produtos disponíveis, um momento...",
        "list_product":      "📝 Iniciando o cadastro do produto, um momento...",
        "pesquisar_web":     "🌐 Consultando informações na web, um momento...",
        "verificar_restricao": "⏳ Verificando restrições, um momento...",
    }

    async def handle_message(self, phone: str, text: str, send_fn=None) -> str:
        """Processes the user message and returns the final reply.

        send_fn: optional coroutine (phone, text) → None.
        When provided, intermediate messages (e.g. "please wait, searching...")
        are proactively sent before slow tools without waiting for the user to ask again.
        """
        db = self._db or get_db()

        if db is None:
            return await self._no_db_fallback(phone, text)

        user_repo, listing_repo, neg_repo, tx_repo, delivery_repo, conv_repo = self._repos(db)
        engine    = NegotiationEngine(db)
        flow_repo = ListingFlowRepository(db)

        user    = await user_repo.find_or_create_by_phone(phone)
        user_id = user["id"]

        # Check if there is an active product listing flow for this phone
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

        # Load data in parallel — needed in all response paths
        active_negs, seller_profile, history = await _gather(
            neg_repo.find_active_by_buyer(user_id),
            user_repo.get_seller_profile(user_id),
            conv_repo.get_history(user_id),
        )

        # Rich context with real DB data — the LLM always works with current info
        context = self._build_context(user, active_negs, seller_profile, phone=phone)

        # Pending business confirmations (e.g. confirm listing price)
        pending = PENDING_CONFIRMATIONS.get(phone)
        if pending:
            reply = await self._handle_confirmation(
                phone, text, pending, user, user_repo, listing_repo,
                neg_repo, tx_repo, delivery_repo, engine,
                history=history, context=context,
            )
            await conv_repo.add(user_id, "user", text)
            await conv_repo.add(user_id, "assistant", reply)
            return reply

        # Phase 1: LLM sees full history and decides which tools to call
        messages, tool_calls = await self._conv.get_tool_calls(
            contexto=context,
            history=history,
            user_message=text,
            tools=NOTHA_TOOLS,
        )

        # If there are slow tools, send a "please wait" message immediately
        # before executing them — so the user knows something is happening
        if send_fn and tool_calls:
            slow_tools = [tc for tc in tool_calls if tc["name"] in self._SLOW_TOOLS]
            if slow_tools:
                interim_text = next(
                    (m.get("content") for m in reversed(messages)
                     if m["role"] == "assistant" and m.get("content")),
                    None,
                )
                if not interim_text:
                    interim_text = self._WAIT_MSG_FALLBACK.get(slow_tools[0]["name"])
                if interim_text:
                    try:
                        await send_fn(phone, interim_text)
                        logger.info("Interim message sent to %s: %s", phone, interim_text[:60])
                    except Exception as e:
                        logger.warning("Failed to send interim message: %s", e)

        # Tools that modify user data in the DB — require reloading context
        _USER_DATA_TOOLS = {
            "update_name", "update_nickname", "update_tax_id",
            "update_pix_key", "update_address", "update_location",
        }

        # Code deterministically executes what the LLM decided
        # and returns the real DB result for the LLM to generate an accurate response
        tool_results: dict[str, str] = {}
        override_reply: str | None = None
        user_data_changed = False

        for tc in tool_calls:
            if tc["name"] in _USER_DATA_TOOLS:
                user_data_changed = True
            result_text, complex_reply = await self._execute_tool(
                tc, phone, text, user,
                user_repo, listing_repo, neg_repo, engine, active_negs,
                history=history, context=context,
            )
            tool_results[tc["id"]] = result_text
            if complex_reply is not None:
                override_reply = complex_reply

        # If any tool updated user data, reload from DB
        # so the context passed to the LLM in phase 2 reflects the actual state
        if user_data_changed:
            user = await user_repo.find_by_id(user_id) or user
            seller_profile = await user_repo.get_seller_profile(user_id)
            context = self._build_context(user, active_negs, seller_profile, phone=phone)

        if override_reply:
            final_reply = override_reply
        elif tool_calls:
            # Phase 2: LLM receives real results and generates a natural response
            final_reply = await self._conv.get_reply_after_tools(messages, tool_results, contexto=context)
        else:
            last_assistant = next(
                (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
                None,
            )
            if last_assistant:
                final_reply = last_assistant
            else:
                final_reply, _ = await self._conv.chat_with_tools(
                    contexto=context,
                    history=history,
                    user_message=text,
                    tools=None,
                )

            # If there is an active negotiation, check if this is a confirmation/rejection
            if active_negs:
                neg_reply = await self._check_negotiation_response(
                    phone, text, user, active_negs[0],
                    user_repo, neg_repo, listing_repo, engine,
                    history=history,
                )
                if neg_reply:
                    final_reply = neg_reply

        # Persist message and reply in DB
        await conv_repo.add(user_id, "user", text)
        await conv_repo.add(user_id, "assistant", final_reply)
        return final_reply

    async def _execute_tool(
        self, tc: dict, phone: str, text: str, user,
        user_repo: UserRepository, listing_repo: ListingRepository,
        neg_repo: NegotiationRepository, engine: NegotiationEngine,
        active_negs: list,
        history: list[dict] | None = None,
        context: str = "",
    ) -> tuple[str, str | None]:
        """Deterministically executes the tool chosen by the LLM.

        Returns (result_text, complex_reply):
        - result_text: real DB result, passed back to the LLM for accurate response generation
        - complex_reply: str if the flow produces its own reply (list, search), None otherwise
        """
        name = tc["name"]
        args = tc["arguments"]

        if name == "update_name":
            name_val = args.get("name", "").strip()
            if _is_valid_name(name_val):
                await user_repo.update(user["id"], full_name=name_val)
                updated_user     = await user_repo.find_by_id(user["id"])
                saved_name       = updated_user.get("full_name") if updated_user else name_val
                saved_nickname   = updated_user.get("nickname")  if updated_user else ""
                cpf_registered   = bool(updated_user.get("tax_id")) if updated_user else False
                logger.info("Name updated via tool: '%s' (user_id=%s)", saved_name, user["id"])
                result = (
                    f"Nome legal salvo no banco: '{saved_name}'. "
                    f"Apelido: '{saved_nickname or 'não definido'}'. "
                    f"CPF: {'registrado' if cpf_registered else 'ainda não registrado'}."
                )
            else:
                logger.warning("Name rejected by validation: '%s'", name_val)
                result = (
                    f"Nome '{name_val}' rejeitado (parece saudação ou inválido). "
                    f"Nome atual no banco: '{user.get('full_name') or 'vazio'}'."
                )
            return result, None

        if name == "update_nickname":
            nickname = args.get("nickname", "").strip()
            if nickname and len(nickname) >= 2:
                await user_repo.update_nickname(user["id"], nickname)
                logger.info("Nickname updated via tool: '%s' (user_id=%s)", nickname, user["id"])
                result = (
                    f"Apelido salvo no banco: '{nickname}'. "
                    f"O usuário agora será chamado de '{nickname}'. "
                    f"Nome legal permanece: '{user.get('full_name') or 'não informado'}'."
                )
            else:
                result = "Apelido vazio ou muito curto — nenhuma alteração feita."
            return result, None

        if name == "update_tax_id":
            raw_cpf = args.get("tax_id", "").strip()
            cpf     = re.sub(r"[\.\-\s]", "", raw_cpf)
            if _looks_like_cpf(cpf):
                existing = await user_repo.find_by_tax_id(cpf)
                if existing and existing["id"] != user["id"]:
                    await user_repo.add_phone(existing["id"], phone)
                    logger.info("CPF already existed — phone transferred to user_id=%s", existing["id"])
                    result = f"CPF já estava cadastrado para '{existing.get('full_name') or 'usuário'}'. Histórico recuperado."
                else:
                    await user_repo.update(user["id"], tax_id=cpf)
                    logger.info("CPF updated via tool (user_id=%s)", user["id"])
                    result = f"CPF '{cpf}' salvo no banco para user_id={user['id']}. Nome: '{user.get('full_name') or 'vazio'}'."
            else:
                logger.warning("Invalid CPF received: '%s'", raw_cpf)
                result = f"CPF '{raw_cpf}' inválido (precisa ter 11 dígitos). CPF atual no banco: {'registrado' if user.get('tax_id') else 'vazio'}."
            return result, None

        if name == "update_pix_key":
            pix_key = args.get("pix_key", "").strip()
            if pix_key:
                await user_repo.upsert_seller_profile(user["id"], pix_key=pix_key)
                logger.info("Pix key updated (user_id=%s)", user["id"])
                result = f"Chave Pix '{pix_key}' salva no banco para user_id={user['id']}."
            else:
                result = "Chave Pix vazia — nenhuma alteração feita."
            return result, None

        if name == "update_address":
            address = args.get("address", "").strip()
            if address:
                await user_repo.upsert_seller_profile(user["id"], pickup_address=address)
                logger.info("Address updated (user_id=%s)", user["id"])
                result = f"Endereço de retirada '{address}' salvo no banco para user_id={user['id']}."
            else:
                result = "Endereço vazio — nenhuma alteração feita."
            return result, None

        if name == "update_location":
            city         = args.get("city", "").strip()         or None
            neighborhood = args.get("neighborhood", "").strip() or None
            if city or neighborhood:
                await user_repo.update_location(user["id"], city=city, neighborhood=neighborhood)
                logger.info(
                    "Location updated (user_id=%s): city=%s neighborhood=%s",
                    user["id"], city, neighborhood,
                )
                parts = []
                if city:
                    parts.append(f"cidade='{city}'")
                if neighborhood:
                    parts.append(f"bairro='{neighborhood}'")
                result = f"Localização salva no banco: {', '.join(parts)}. Usar para filtrar buscas de produtos."
            else:
                result = "Nenhuma localização informada — nenhuma alteração feita."
            return result, None

        if name == "save_interest":
            description = args.get("search_description", "").strip()
            if not description:
                return "Descrição de busca vazia — interesse não salvo.", None
            db = self._db or get_db()
            if db:
                search_repo  = SavedSearchRepository(db)
                alert_record = await search_repo.create(
                    user_id=user["id"],
                    phone=phone,
                    search_description=description,
                    category=args.get("category", "").strip() or None,
                    search_city=args.get("search_city", "").strip() or None,
                    search_neighborhood=args.get("search_neighborhood", "").strip() or None,
                )
                logger.info(
                    "Interest alert saved (user_id=%s): '%s' id=%s",
                    user["id"], description, alert_record["id"],
                )
                result = (
                    f"Alerta de interesse salvo (id={alert_record['id']}): '{description}'. "
                    "Usuário será notificado via WhatsApp assim que aparecer um produto compatível."
                )
            else:
                result = "Banco indisponível — interesse não salvo."
            return result, None

        if name == "cancel_alerts":
            db = self._db or get_db()
            if db:
                search_repo = SavedSearchRepository(db)
                alerts      = await search_repo.find_by_user(user["id"])
                await search_repo.cancel_all_by_user(user["id"])
                logger.info("Alerts cancelled (user_id=%s): %d alerts", user["id"], len(alerts))
                result = f"{len(alerts)} alerta(s) de busca cancelado(s) para user_id={user['id']}."
            else:
                result = "Banco indisponível — alertas não cancelados."
            return result, None

        if name == "list_product":
            db = self._db or get_db()
            complex_reply = await self._start_listing_flow(
                phone=phone,
                text=text,
                user=user,
                user_repo=user_repo,
                db=db,
                history=history or [],
                context=context,
            )
            return "fluxo de cadastro iniciado", complex_reply

        if name == "search_product":
            intent = {
                "category":           args.get("category"),
                "search_description": args.get("search_description"),
                "search_city":        args.get("search_city", "").strip() or None,
                "search_neighborhood": args.get("search_neighborhood", "").strip() or None,
            }
            complex_reply = await self._handle_search(
                phone, text, user, listing_repo, intent,
                history=history or [], context=context,
            )
            return "busca executada", complex_reply

        if name in _BUILTIN_TOOL_MAP:
            result = await _BUILTIN_TOOL_MAP[name].execute(**args)
            logger.info("Built-in tool '%s' executed successfully", name)
            return result, None

        logger.warning("Unknown tool called by LLM: %s", name)
        return f"ferramenta '{name}' desconhecida", None

    async def _check_negotiation_response(
        self, phone: str, text: str, user, neg,
        user_repo, neg_repo, listing_repo, engine,
        history: list[dict] | None = None,
    ) -> str | None:
        """Checks whether the message is a confirmation/rejection of an active negotiation."""
        intent      = await self._conv.extract_intent(text, contexto="negociacao_ativa")
        intent_type = intent.get("intencao", "outro")
        if intent_type in ("confirmacao", "recusa"):
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
        """Builds context with real DB data for the LLM.

        Includes name, nickname, tax_id, identity verification, seller profile
        and active negotiations — all from the DB, nothing invented.
        """
        parts = []

        full_name       = user.get("full_name") or ""
        nickname        = user.get("nickname")  or ""
        tax_id          = user.get("tax_id")    or ""
        user_id         = user.get("id", "?")
        identity_status = user.get("identity_status") or "unverified"

        # Legal name
        if not full_name:
            parts.append("STATUS: usuário sem nome cadastrado — peça o nome completo")
        elif not _is_valid_name(full_name):
            parts.append(
                f"STATUS: nome='{full_name}' parece incorreto — "
                "capture o nome real se o usuário mencionar"
            )
        else:
            parts.append(f"nome: {full_name}")

        # Nickname (how to address the user)
        if nickname:
            parts.append(f"apelido: {nickname}")
        else:
            parts.append("apelido: não definido")

        # Tax ID and identity verification
        parts.append(f"CPF: {'registrado (✓)' if tax_id else 'não registrado'}")

        _IDENTITY_LABEL = {
            "unverified":  "não verificado",
            "under_review": "em análise (documento enviado)",
            "verified":    "verificado (✓)",
            "rejected":    "rejeitado (documento inválido — peça novo envio)",
        }
        parts.append(f"identidade: {_IDENTITY_LABEL.get(identity_status, identity_status)}")
        parts.append(f"user_id: {user_id}")

        # User's home address (city/neighborhood — for deliveries)
        home_city         = user.get("city")         or ""
        home_neighborhood = user.get("neighborhood") or ""
        if home_city or home_neighborhood:
            loc_parts = []
            if home_neighborhood:
                loc_parts.append(f"bairro={home_neighborhood}")
            if home_city:
                loc_parts.append(f"cidade={home_city}")
            parts.append(
                f"mora em: {', '.join(loc_parts)} "
                "(endereço de moradia — para entregas; região de BUSCA é perguntada na hora)"
            )
        else:
            parts.append(
                "mora em: não informado (se quiser usar como referência de busca, pergunte cidade/bairro de moradia)"
            )

        # Seller profile
        if seller_profile:
            pix_key = seller_profile.get("pix_key")     or ""
            address = seller_profile.get("pickup_address") or ""
            parts.append(f"chave Pix: {pix_key if pix_key else 'não cadastrada'}")
            if address:
                parts.append(f"endereço retirada: {address}")
        else:
            parts.append("perfil vendedor: não criado")

        # Active negotiations
        if active_negs:
            neg = active_negs[0]
            parts.append(
                f"negociação ativa: id={neg['id']}, status={neg['status']}, "
                f"valor=R${neg.get('current_price', 0):.2f}"
            )
        else:
            parts.append("sem negociações ativas")

        # Inferred timezone: registered city > DDI+area code > DDI
        city_for_tz = home_city or ""
        tz = infer_timezone(phone, cidade=city_for_tz)
        parts.append(f"fuso_horario: {tz}")

        return " | ".join(parts)

    async def _handle_list_product(
        self, phone, text, user, user_repo, listing_repo, intent,
        history: list[dict] | None = None, context: str = "",
    ) -> str:
        check = await user_repo.check_missing_fields(user["id"], "list_product")
        if check["missing"]:
            missing_fields = ", ".join(check["missing"])
            return await self._conv.speak(
                f"Para listar um produto, preciso de: {missing_fields}. Peça de forma natural.",
                history, context,
            )

        description    = intent.get("description", text)
        category       = intent.get("category")
        informed_price = intent.get("asking_price")

        similar_history = []
        if category:
            rows = await listing_repo.find_similar_sold(category)
            similar_history = [dict(r) for r in rows]

        appraisal = await self._pricing.appraise_with_web_search(
            descricao=description,
            categoria=category,
            preco_informado_vendedor=informed_price,
            historico_similares=similar_history,
        )

        PENDING_CONFIRMATIONS[phone] = {
            "tipo":           "confirmar_preco_listing",
            "appraisal":      appraisal,
            "description":    description,
            "category":       category,
            "asking_price":   informed_price,
            "seller_id":      user["id"],
        }

        price_alert = ""
        if appraisal.get("alerta_preco_vendedor"):
            price_alert = " (Atenção: o preço que você informou difere muito do mercado!)"

        return await self._conv.speak(
            f"Avaliei o produto. Preço sugerido: R${appraisal['preco_sugerido']:.2f}{price_alert}. "
            f"Justificativa: {appraisal['justificativa']}. "
            f"Comunique o preço sugerido e pergunte se confirma o anúncio por esse valor "
            f"(mínimo interno: R${appraisal['preco_minimo_sugerido']:.2f}). Termine com pergunta de confirmação sim/não.",
            history, context,
        )

    async def _handle_search(
        self, phone, text, user, listing_repo, intent,
        history: list[dict] | None = None, context: str = "",
    ) -> str:
        history           = history or []
        category          = intent.get("category")
        search_desc       = intent.get("search_description") or category or "produto"
        city_filter       = intent.get("search_city")
        neighborhood_filter = intent.get("search_neighborhood")

        # Level 1: search with full filter (neighborhood + city)
        listings = await listing_repo.find_available(
            categoria=category, limit=5,
            cidade=city_filter, bairro=neighborhood_filter,
        )
        if listings:
            region_label = (
                f"no bairro {neighborhood_filter}" if neighborhood_filter else
                f"em {city_filter}" if city_filter else
                "disponíveis"
            )
            return await self._format_search_results(listings, region_label, history, context)

        # Level 2: try just the city
        if neighborhood_filter and city_filter:
            listings = await listing_repo.find_available(
                categoria=category, limit=5, cidade=city_filter,
            )
            if listings:
                prefix = f"Nada no {neighborhood_filter}, mas achei em {city_filter}:"
                return await self._format_search_results(
                    listings, f"em {city_filter}", history, context, prefixo=prefix
                )

        # Level 3: try all of Brazil
        if city_filter or neighborhood_filter:
            listings = await listing_repo.find_available(categoria=category, limit=5)
            original_region = neighborhood_filter or city_filter or "essa região"
            if listings:
                prefix = f"Não encontrei nada em {original_region}. Mas tem isso disponível em outras regiões:"
                return await self._format_search_results(
                    listings, "em outras regiões", history, context, prefixo=prefix
                )

        # Nothing anywhere
        original_region = neighborhood_filter or city_filter or "qualquer região"
        return await self._conv.speak(
            f"Nenhum '{search_desc}' disponível agora em {original_region}. "
            f"Informe isso e pergunte se quer salvar um alerta para ser avisado quando aparecer.",
            history, context,
        )

    async def _notify_interested_users(self, listing: dict, db: DB) -> None:
        """Checks saved searches and notifies via WhatsApp anyone interested in this listing."""
        try:
            from whatsapp import send_message as _wpp_send
            search_repo = SavedSearchRepository(db)
            alerts      = await search_repo.find_active()
            for alert in alerts:
                if not search_repo.matches(alert, listing):
                    continue
                product_name = listing.get("description") or "Produto"
                city         = listing.get("seller_city") or ""
                price        = listing.get("listed_price") or 0
                loc_text     = f" em {city}" if city else ""
                notification = (
                    f"Achei um produto que pode te interessar{loc_text}!\n\n"
                    f"📦 {product_name}\n"
                    f"💰 R${price:.2f}\n\n"
                    f"Quer ver mais detalhes ou negociar? É só responder aqui!"
                )
                try:
                    await _wpp_send(alert["phone"], notification)
                    await search_repo.record_notification(alert["id"])
                    logger.info(
                        "Notification sent: alert_id=%s phone=%s listing_id=%s",
                        alert["id"], alert["phone"], listing.get("id"),
                    )
                except Exception as e:
                    logger.warning("Failed to notify alert_id=%s: %s", alert["id"], e)
        except Exception as e:
            logger.error("Error in _notify_interested_users: %s", e)

    async def _format_search_results(
        self, listings: list, regiao_label: str,
        history: list[dict] | None = None, context: str = "", prefixo: str = "",
    ) -> str:
        items = [
            f"• {l['description']} — R${l['listed_price']:.2f}"
            f" ({l.get('seller_city') or 'localização não informada'})"
            for l in listings
        ]
        body = "\n".join(items)
        instruction = (
            f"{prefixo}\n{body}".strip() if prefixo
            else f"Encontrei {len(listings)} produto(s) {regiao_label}:\n{body}"
        )
        instruction += "\n\nPergunte se o usuário quer negociar algum."
        return await self._conv.speak(instruction, history or [], context)

    async def _handle_negotiation_response(
        self, phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine,
        history: list[dict] | None = None,
        context: str = "",
    ) -> str:
        history  = history or []
        accepted = intent.get("aceitou", False)
        status   = neg["status"]

        if status == "pending_seller":
            if accepted:
                await engine.accept_seller_proposal(neg["id"])
                return await self._conv.speak(
                    f"Proposta de R${neg['current_price']:.2f} confirmada. Comunique de forma positiva e informe que o comprador será notificado.",
                    history, context,
                )
            else:
                await engine.reject_seller_proposal(neg["id"])
                return await self._conv.speak(
                    "Proposta recusada. Informe que vai renegociar com o comprador e trazer uma nova proposta.",
                    history, context,
                )

        if status == "pending_buyer":
            if accepted:
                await engine.accept_buyer_proposal(neg["id"])
                return await self._conv.speak(
                    f"Negócio fechado em R${neg['current_price']:.2f}! Comunique o fechamento e informe que o link de pagamento será gerado.",
                    history, context,
                )
            else:
                await engine.reject_buyer_proposal(neg["id"])
                return await self._conv.speak(
                    "Proposta recusada pelo comprador. Informe que vai tentar uma nova rodada de negociação.",
                    history, context,
                )

        reply, _ = await self._conv.chat_with_tools(
            contexto=context or f"negociação status={status}",
            history=history,
            user_message=intent.get("descricao", ""),
            tools=None,
        )
        return reply

    async def _handle_confirmation(
        self, phone, text, pending, user, user_repo, listing_repo,
        neg_repo, tx_repo, delivery_repo, engine,
        history: list[dict] | None = None, context: str = "",
    ) -> str:
        history   = history or []
        conf_type = pending.get("tipo")
        intent    = await self._conv.extract_intent(text, contexto="confirmacao")
        accepted  = intent.get("aceitou", False)

        if conf_type == "confirmar_preco_listing":
            PENDING_CONFIRMATIONS.pop(phone, None)
            if not accepted:
                return await self._conv.speak(
                    "Usuário não confirmou o preço. Peça de forma natural que informe o preço que prefere anunciar.",
                    history, context,
                )
            appraisal = pending["appraisal"]
            listing   = await listing_repo.create(
                seller_id=pending["seller_id"],
                description=pending["description"],
                category=pending.get("category"),
                asking_price=pending.get("asking_price"),
                suggested_price=appraisal["preco_sugerido"],
                listed_price=appraisal["preco_sugerido"],
                floor_price=appraisal["preco_minimo_sugerido"],
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
                history, context,
            )

        PENDING_CONFIRMATIONS.pop(phone, None)
        return await self._conv.speak("Ação cancelada. Informe de forma natural.", history, context)

    # ─────────────────────────────────────────────
    # Product listing flow
    # ─────────────────────────────────────────────

    async def _start_listing_flow(
        self, phone: str, text: str, user, user_repo: UserRepository, db: DB,
        history: list[dict] | None = None, context: str = "",
    ) -> str:
        """Starts the product listing flow (state machine persisted in the DB)."""
        check = await user_repo.check_missing_fields(user["id"], "list_product")
        if check["missing"]:
            missing_fields = ", ".join(check["missing"])
            return await self._conv.speak(
                f"Para listar um produto, preciso de: {missing_fields}. Peça de forma natural.",
                history or [], context,
            )

        flow_repo = ListingFlowRepository(db)

        # Cancel any stuck flow (step != done)
        await flow_repo.cancel(phone)

        # Create new flow
        await flow_repo.create(user["id"], phone)

        first_question = await self._listing_flow_agent.start()
        return first_question

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
        """Routes the text message to the listing flow agent."""
        seller_profile = await user_repo.get_seller_profile(user["id"])
        sp = dict(seller_profile) if seller_profile else {}

        data, photos, reply, completed = await self._listing_flow_agent.handle_message(
            flow=flow,
            text=text,
            seller_profile=sp,
            db=db,
        )

        next_step = data.get("step_next", flow["step"])
        await flow_repo.update_step(flow["id"], next_step, data, photos)

        # Persist history
        await conv_repo.add(user["id"], "user", text)

        # Auto-processing step: send "please wait" → process → return confirmation
        if next_step == "processing":
            if reply:
                from whatsapp import send_message as _wpp_send
                await _wpp_send(phone, reply)

            updated_flow = await flow_repo.get_active(phone)
            if updated_flow:
                processed_data, confirm_msg = await self._listing_flow_agent.processar(
                    flow=dict(updated_flow),
                    listing_repo=listing_repo,
                    db=db,
                )
                current_photos    = _parse_jsonb(updated_flow.get("photos"), [])
                next_step_proc    = processed_data.get("step_next", "confirm")
                await flow_repo.update_step(updated_flow["id"], next_step_proc, processed_data, current_photos)
                await conv_repo.add(user["id"], "assistant", confirm_msg)
                return confirm_msg
            return reply or "Processando seu produto..."

        # Flow confirmed: create listing in DB
        if completed:
            result_msg = await self._finalize_listing(
                flow_id=flow["id"],
                data=data,
                photos=photos,
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
        data: dict,
        photos: list,
        user,
        listing_repo: ListingRepository,
        flow_repo: ListingFlowRepository,
    ) -> str:
        """Creates the listing in the DB with all collected data and marks the flow as done."""
        appraisal    = data.get("appraisal", {})
        product_name = " ".join(
            filter(None, [data.get("brand"), data.get("model"), data.get("version")])
        ) or data.get("description", "Produto")

        listing = await listing_repo.create(
            seller_id=user["id"],
            description=data.get("description", product_name),
            category=data.get("category"),
            photos=[f["media_id"] for f in photos if f.get("media_id")],
            asking_price=data.get("asking_price"),
            suggested_price=appraisal.get("preco_sugerido"),
            listed_price=data.get("listed_price") or appraisal.get("preco_sugerido", 0),
            floor_price=data.get("floor_price") or appraisal.get("preco_minimo_sugerido", 0),
            appraisal_data=appraisal,
            brand=data.get("brand"),
            model=data.get("model"),
            version=data.get("version"),
            usage_state=data.get("usage_state"),
            condition=data.get("condition"),
            has_receipt=data.get("has_receipt"),
            seller_min_price=data.get("seller_min_price"),
            web_info=data.get("web_info"),
            seller_city=data.get("seller_city"),
            vision_analysis=data.get("vision_analysis"),
        )

        await flow_repo.mark_done(flow_id)

        db = self._db or get_db()
        if db:
            import asyncio as _asyncio
            _asyncio.create_task(self._notify_interested_users(dict(listing), db))

        price = data.get("listed_price") or appraisal.get("preco_sugerido", 0) or 0
        return (
            f"Produto anunciado com sucesso! ID #{listing['id']}.\n"
            f"Nome: {product_name}\n"
            f"Preço: R${price:.2f}\n"
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
        Central media router.

        Priority:
        1. If there is an active listing flow at the 'photos_upload' step → routes to listing agent
        2. Otherwise → identifies as identity document
        """
        db = self._db or get_db()
        if db is None:
            return "Recebi sua imagem! Mas estou com problema técnico — tenta de novo em instantes."

        user_repo, listing_repo, *_, conv_repo = self._repos(db)
        flow_repo = ListingFlowRepository(db)

        user = await user_repo.find_or_create_by_phone(phone)

        active_flow = await flow_repo.get_active(phone)
        if active_flow and active_flow["step"] == "photos_upload":
            current_photos, reply = await self._listing_flow_agent.handle_media(
                flow=dict(active_flow),
                media_id=media_id,
                mime_type=mime_type,
                caption=caption or "",
            )
            current_data = _parse_jsonb(active_flow.get("data"), {})
            await flow_repo.update_step(active_flow["id"], "photos_upload", current_data, current_photos)
            if reply:
                await conv_repo.add(user["id"], "assistant", reply)
            return reply

        # Fallback: treat as identity document
        return await self.handle_identity_document(phone, media_id, mime_type, caption)

    async def handle_identity_document(
        self,
        phone: str,
        media_id: str,
        mime_type: str,
        caption: str,
    ) -> str:
        """Processes image/document sent by the user as an identity document.

        Flow:
        1. Finds user in DB (creates if first contact)
        2. Detects document type from caption (national_id, drivers_license, passport)
        3. Downloads image from WhatsApp and uploads to Supabase Storage
        4. Registers in DB and updates identity_status to 'under_review'
        5. Returns a natural-language message to the user
        """
        from storage.identity import processar_documento_identidade

        db = self._db or get_db()
        if db is None:
            return "Recebi seu documento! Mas estou com problema técnico no momento — tenta de novo em instantes."

        user_repo, *_ = self._repos(db)
        user = await user_repo.find_or_create_by_phone(phone)
        user_id      = user["id"]
        display_name = user.get("nickname") or (user.get("full_name") or "").split()[0] or ""

        doc_type = _detect_document_type(caption or "")

        try:
            result = await processar_documento_identidade(
                user_id=user_id,
                media_id=media_id,
                tipo=doc_type,
                user_repo=user_repo,
            )
            logger.info(
                "Identity document saved: user_id=%s type=%s doc_id=%s path=%s",
                user_id, doc_type, result.get("doc_id"), result.get("object_path"),
            )
        except Exception as e:
            logger.error("Failed to process identity document (user_id=%s): %s", user_id, e)
            return (
                "Recebi a imagem, mas houve um problema técnico ao salvá-la. "
                "Pode enviar de novo? Se persistir, tente em formato JPG ou PNG."
            )

        prefix    = f"{display_name}, " if display_name else ""
        doc_label = {
            "national_id":      "RG",
            "drivers_license":  "CNH",
            "passport":         "passaporte",
        }.get(doc_type, "documento")

        return (
            f"{prefix}recebi seu {doc_label}! "
            "Vou analisar e te aviso assim que a verificação for concluída. "
            "Normalmente leva até 1 dia útil."
        )

    async def reset(self, phone: str) -> None:
        """Clears DB history and memory; removes pending confirmations."""
        db = self._db or get_db()
        PENDING_CONFIRMATIONS.pop(phone, None)
        _MEMORY_HISTORY.pop(phone, None)
        if db is None:
            return
        user_repo, *_, conv_repo = self._repos(db)
        user = await user_repo.find_by_phone(phone)
        if user:
            await conv_repo.clear(user["id"])
            logger.info("History cleared for user_id=%s", user["id"])

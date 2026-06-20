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
    UserRepository, ListingRepository, NegotiationRepository,
    TransactionRepository, DeliveryRepository,
)
from agents.conversation import ConversationAgent, NOTHA_TOOLS
from agents.pricing import PricingAgent
from agents.logistics import LogisticsAgent
from engine.negotiation import NegotiationEngine

logger = logging.getLogger("notha.orchestrator")

CONVERSATION_HISTORY: dict[str, list[dict]] = {}
PENDING_CONFIRMATIONS: dict[str, dict] = {}
PROCESSED_MESSAGE_IDS: set[str] = set()
MAX_HISTORY = 20
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


def _add_to_history(phone: str, role: str, content: str) -> None:
    if phone not in CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[phone] = []
    CONVERSATION_HISTORY[phone].append({"role": role, "content": content})
    if len(CONVERSATION_HISTORY[phone]) > MAX_HISTORY:
        CONVERSATION_HISTORY[phone] = CONVERSATION_HISTORY[phone][-MAX_HISTORY:]


def _get_history(phone: str) -> list[dict]:
    return CONVERSATION_HISTORY.get(phone, [])


def _clear_history(phone: str) -> None:
    CONVERSATION_HISTORY.pop(phone, None)
    PENDING_CONFIRMATIONS.pop(phone, None)


class Orchestrator:
    def __init__(self, db: DB | None = None):
        self._db = db
        self._conv = ConversationAgent()
        self._pricing = PricingAgent(db)

    def _repos(self, db: DB):
        return (
            UserRepository(db),
            ListingRepository(db),
            NegotiationRepository(db),
            TransactionRepository(db),
            DeliveryRepository(db),
        )

    async def handle_message(self, phone: str, text: str) -> str:
        db = self._db or get_db()

        if db is None:
            return await self._no_db_fallback(phone, text)

        user_repo, listing_repo, neg_repo, tx_repo, delivery_repo = self._repos(db)
        engine = NegotiationEngine(db)

        user = await user_repo.find_or_create_by_phone(phone)

        # Confirmações pendentes de negócio (ex: confirmar preço de anúncio)
        pending = PENDING_CONFIRMATIONS.get(phone)
        if pending:
            return await self._handle_confirmation(
                phone, text, pending, user, user_repo, listing_repo,
                neg_repo, tx_repo, delivery_repo, engine,
            )

        active_negs = await neg_repo.find_active_by_buyer(user["id"])
        seller_profile = await user_repo.get_seller_profile(user["id"])

        # Contexto rico com dados reais do banco — o LLM sempre trabalha com info atual
        contexto = self._build_context(user, active_negs, seller_profile)

        # Fase 1: LLM vê o histórico completo e decide quais ferramentas chamar
        messages, tool_calls = await self._conv.get_tool_calls(
            contexto=contexto,
            history=_get_history(phone),
            user_message=text,
            tools=NOTHA_TOOLS,
        )

        # Código executa deterministicamente o que o LLM decidiu
        # e devolve o resultado real do banco para o LLM gerar resposta precisa
        tool_results: dict[str, str] = {}
        override_reply: str | None = None

        for tc in tool_calls:
            result_text, complex_reply = await self._execute_tool(
                tc, phone, text, user,
                user_repo, listing_repo, neg_repo, engine, active_negs,
            )
            tool_results[tc["id"]] = result_text
            if complex_reply is not None:
                override_reply = complex_reply

        if override_reply:
            # Fluxos complexos (listar, buscar) têm reply próprio
            final_reply = override_reply
        elif tool_calls:
            # Fase 2: LLM recebe os resultados reais e gera resposta natural
            final_reply = await self._conv.get_reply_after_tools(messages, tool_results)
        else:
            # Sem tool calls: resposta já está na última mensagem do assistant
            # (chat_with_tools retornou o texto diretamente na fase 1)
            last_assistant = next(
                (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
                None,
            )
            if last_assistant:
                final_reply = last_assistant
            else:
                # Fallback: gera resposta sem tools
                final_reply, _ = await self._conv.chat_with_tools(
                    contexto=contexto,
                    history=_get_history(phone),
                    user_message=text,
                    tools=None,
                )

            # Se há negociação ativa, verifica se é confirmação/recusa
            if active_negs:
                neg_reply = await self._check_negotiation_response(
                    phone, text, user, active_negs[0],
                    user_repo, neg_repo, listing_repo, engine,
                )
                if neg_reply:
                    final_reply = neg_reply

        _add_to_history(phone, "user", text)
        _add_to_history(phone, "assistant", final_reply)
        return final_reply

    async def _execute_tool(
        self, tc: dict, phone: str, text: str, user,
        user_repo: UserRepository, listing_repo: ListingRepository,
        neg_repo: NegotiationRepository, engine: NegotiationEngine,
        active_negs: list,
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

        if name == "listar_produto":
            intent = {
                "descricao": args.get("descricao", text),
                "categoria": args.get("categoria"),
                "preco_informado": args.get("preco_informado"),
            }
            complex_reply = await self._handle_list_product(phone, text, user, user_repo, listing_repo, intent)
            return "fluxo de listagem iniciado", complex_reply

        if name == "buscar_produto":
            intent = {
                "categoria": args.get("categoria"),
                "descricao_busca": args.get("descricao_busca"),
            }
            complex_reply = await self._handle_search(phone, text, user, listing_repo, intent)
            return "busca executada", complex_reply

        logger.warning("Ferramenta desconhecida chamada pelo LLM: %s", name)
        return f"ferramenta '{name}' desconhecida", None

    async def _check_negotiation_response(
        self, phone: str, text: str, user, neg,
        user_repo, neg_repo, listing_repo, engine,
    ) -> str | None:
        """Verifica se a mensagem é uma confirmação/recusa de negociação ativa."""
        intent = await self._conv.extract_intent(text, contexto="negociacao_ativa")
        intencao = intent.get("intencao", "outro")
        if intencao in ("confirmacao", "recusa"):
            return await self._handle_negotiation_response(
                phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine,
            )
        return None

    async def _no_db_fallback(self, phone: str, text: str) -> str:
        messages, tool_calls = await self._conv.get_tool_calls(
            contexto="sem banco de dados disponível — modo memória apenas",
            history=_get_history(phone),
            user_message=text,
            tools=NOTHA_TOOLS,
        )
        # Sem banco, não executamos tools — apenas pegamos o texto já gerado
        last_assistant = next(
            (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
            "Tive um problema técnico. Tente de novo em instantes!",
        )
        _add_to_history(phone, "user", text)
        _add_to_history(phone, "assistant", last_assistant)
        return last_assistant

    def _build_context(self, user, active_negs: list, seller_profile=None) -> str:
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
            parts.append(f"apelido: {apelido} (chame-o assim)")
        else:
            parts.append("apelido: não definido (use o primeiro nome ou pergunte como prefere ser chamado)")

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

        return " | ".join(parts)

    async def _handle_list_product(self, phone, text, user, user_repo, listing_repo, intent) -> str:
        check = await user_repo.check_missing_fields(user["id"], "listar_produto")
        if check["falta"]:
            campos = ", ".join(check["falta"])
            return await self._conv.build_reply(
                f"Para listar um produto, preciso de: {campos}. Pode me informar?",
                {"falta": check["falta"]},
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

        return await self._conv.ask_confirmation(
            f"Avaliei o produto. Preço sugerido: R${appraisal['preco_sugerido']:.2f}{alerta}. "
            f"Justificativa: {appraisal['justificativa']}. "
            f"Confirma o anúncio por R${appraisal['preco_sugerido']:.2f} "
            f"(mínimo interno: R${appraisal['preco_minimo_sugerido']:.2f})?",
            appraisal,
        )

    async def _handle_search(self, phone, text, user, listing_repo, intent) -> str:
        categoria = intent.get("categoria")
        listings = await listing_repo.find_available(categoria=categoria, limit=5)
        if not listings:
            return await self._conv.build_reply(
                "Não encontrei produtos disponíveis no momento. Quer cadastrar um produto para venda?",
                {},
            )
        items = [
            f"• {l['descricao']} — R${l['preco_anunciado']:.2f} ({l['categoria'] or 'sem categoria'})"
            for l in listings
        ]
        return await self._conv.build_reply(
            f"Encontrei {len(listings)} produto(s) disponível(is):\n" + "\n".join(items) +
            "\n\nQuer negociar algum? Me diz qual te interessa!",
            {},
        )

    async def _handle_negotiation_response(
        self, phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine
    ) -> str:
        aceitou = intent.get("aceitou", False)
        status = neg["status"]

        if status == "proposta_ao_vendedor":
            if aceitou:
                result = await engine.aceitar_proposta_vendedor(neg["id"])
                return await self._conv.build_reply(
                    f"Ótimo! Proposta de R${neg['preco_atual_proposto']:.2f} confirmada. Vou notificar o comprador.",
                    result,
                )
            else:
                await engine.recusar_proposta_vendedor(neg["id"])
                return await self._conv.build_reply(
                    "Entendido! Vou renegociar com o comprador e trazer uma nova proposta.", {}
                )

        if status == "proposta_ao_comprador":
            if aceitou:
                result = await engine.aceitar_proposta_comprador(neg["id"])
                return await self._conv.build_reply(
                    f"Negócio fechado em R${neg['preco_atual_proposto']:.2f}! "
                    "Vou gerar o link de pagamento em seguida.",
                    result,
                )
            else:
                await engine.recusar_proposta_comprador(neg["id"])
                return await self._conv.build_reply(
                    "Beleza! Vou tentar uma nova rodada de negociação.", {}
                )

        reply, _ = await self._conv.chat_with_tools(
            contexto=f"negociação status={status}",
            history=_get_history(phone),
            user_message=intent.get("descricao", ""),
            tools=None,
        )
        return reply

    async def _handle_confirmation(
        self, phone, text, pending, user, user_repo, listing_repo,
        neg_repo, tx_repo, delivery_repo, engine,
    ) -> str:
        tipo = pending.get("tipo")
        intent = await self._conv.extract_intent(text, contexto="confirmacao")
        aceitou = intent.get("aceitou", False)

        if tipo == "confirmar_preco_listing":
            PENDING_CONFIRMATIONS.pop(phone, None)
            if not aceitou:
                return await self._conv.build_reply(
                    "Sem problema! Me diz o preço que você prefere anunciar.", {}
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
            return await self._conv.build_reply(
                f"Produto anunciado! ID #{listing['id']}. "
                f"Preço: R${appraisal['preco_sugerido']:.2f}. "
                "Avisarei quando houver interessados!",
                {"listing_id": listing["id"]},
            )

        PENDING_CONFIRMATIONS.pop(phone, None)
        return await self._conv.build_reply("Ok, ação cancelada.", {})

    def _summarize_negs(self, negs) -> str:
        if not negs:
            return "sem negociação ativa"
        statuses = [n["status"] for n in negs]
        return f"{len(negs)} negociação(ões) ativa(s): {', '.join(statuses)}"

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

    def _summarize_negs(self, negs) -> str:
        if not negs:
            return "sem negociação ativa"
        statuses = [n["status"] for n in negs]
        return f"{len(negs)} negociação(ões) ativa(s): {', '.join(statuses)}"

    async def reset(self, phone: str) -> None:
        _clear_history(phone)

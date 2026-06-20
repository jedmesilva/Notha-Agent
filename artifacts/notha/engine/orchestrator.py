"""
Orquestrador — roteamento central de mensagens.

Recebe toda mensagem entrante, consulta o State Store, decide qual agente aciona,
escreve o resultado de volta. Tem o mínimo de lógica de negócio possível.
"""
import json
import logging
from db.connection import DB, get_db
from db.repositories import (
    UserRepository, ListingRepository, NegotiationRepository,
    TransactionRepository, DeliveryRepository,
)
from agents.conversation import ConversationAgent
from agents.pricing import PricingAgent
from agents.logistics import LogisticsAgent
from engine.negotiation import NegotiationEngine

logger = logging.getLogger("notha.orchestrator")

CONVERSATION_HISTORY: dict[str, list[dict]] = {}
PENDING_CONFIRMATIONS: dict[str, dict] = {}
MAX_HISTORY = 20


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

        user = await user_repo.find_by_phone(phone)

        pending = PENDING_CONFIRMATIONS.get(phone)
        if pending:
            return await self._handle_confirmation(
                phone, text, pending, user, user_repo, listing_repo,
                neg_repo, tx_repo, delivery_repo, engine,
            )

        if not user:
            return await self._onboard_new_user(phone, text, user_repo)

        intent = await self._conv.extract_intent(
            text,
            contexto=self._build_context_string(user, await neg_repo.find_active_by_buyer(user["id"])),
        )

        intencao = intent.get("intencao", "outro")

        if intencao == "listar_produto":
            return await self._handle_list_product(phone, text, user, user_repo, listing_repo, intent)

        if intencao == "buscar_produto":
            return await self._handle_search(phone, text, user, listing_repo, intent)

        if intencao == "informar_dados":
            return await self._handle_data_update(phone, intent, user, user_repo)

        active_negs = await neg_repo.find_active_by_buyer(user["id"])

        if intencao in ("confirmacao", "recusa") and active_negs:
            neg = active_negs[0]
            return await self._handle_negotiation_response(
                phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine,
            )

        reply = await self._conv.respond(
            phone=phone,
            user_message=text,
            history=_get_history(phone),
            role="geral",
            produto_info="sem produto específico",
            status_negociacao=self._summarize_negs(active_negs),
        )
        _add_to_history(phone, "user", text)
        _add_to_history(phone, "assistant", reply)
        return reply

    async def _no_db_fallback(self, phone: str, text: str) -> str:
        """Funciona sem banco de dados — modo memória apenas."""
        reply = await self._conv.respond(
            phone=phone,
            user_message=text,
            history=_get_history(phone),
        )
        _add_to_history(phone, "user", text)
        _add_to_history(phone, "assistant", reply)
        return reply

    async def _onboard_new_user(self, phone: str, text: str, user_repo: UserRepository) -> str:
        intent = await self._conv.extract_intent(text, contexto="primeiro_contato")

        if intent.get("intencao") == "informar_dados" and intent.get("campo") == "cpf":
            cpf = intent.get("valor", "").strip()
            existing = await user_repo.find_by_cpf(cpf)
            if existing:
                await user_repo.add_phone(existing["id"], phone)
                return await self._conv.build_reply(
                    f"Bem-vindo de volta, {existing['nome'] or 'usuário'}! Recuperei seu histórico.",
                    {"user_id": existing["id"]},
                )
            user = await user_repo.create_with_phone(phone)
            await user_repo.update(user["id"], cpf=cpf)
            return await self._conv.build_reply(
                "CPF registrado! Qual é o seu nome?", {}
            )

        if intent.get("intencao") == "informar_dados" and intent.get("campo") == "nome":
            user = await user_repo.create_with_phone(phone, nome=intent.get("valor"))
            return await self._conv.build_reply(
                f"Prazer! Para continuarmos, preciso do seu CPF (só os números).",
                {},
            )

        msg = await self._conv.build_reply(
            "Olá! Sou o NOTHA, agente de compra e venda de produtos via WhatsApp. "
            "Para começar, qual é o seu nome?",
            {},
        )
        return msg

    async def _handle_list_product(
        self, phone, text, user, user_repo, listing_repo, intent
    ) -> str:
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
            alerta = f" (Atenção: o preço que você informou difere muito do mercado!)"

        return await self._conv.ask_confirmation(
            f"Avaliei o produto. Preço sugerido: R${appraisal['preco_sugerido']:.2f}{alerta}. "
            f"Justificativa: {appraisal['justificativa']}. "
            f"Confirma o anúncio por R${appraisal['preco_sugerido']:.2f} (mínimo interno: R${appraisal['preco_minimo_sugerido']:.2f})?",
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
        items = []
        for l in listings:
            items.append(f"• {l['descricao']} — R${l['preco_anunciado']:.2f} ({l['categoria'] or 'sem categoria'})")
        return await self._conv.build_reply(
            f"Encontrei {len(listings)} produto(s) disponível(is):\n" + "\n".join(items) +
            "\n\nQuer negociar algum? Me diga qual te interessa!",
            {},
        )

    async def _handle_data_update(self, phone, intent, user, user_repo) -> str:
        campo = intent.get("campo", "")
        valor = intent.get("valor", "")

        if campo == "nome":
            await user_repo.update(user["id"], nome=valor)
            return await self._conv.build_reply(f"Nome atualizado para {valor}!", {})

        if campo == "cpf":
            await user_repo.update(user["id"], cpf=valor)
            return await self._conv.build_reply("CPF registrado!", {})

        if campo == "endereco":
            await user_repo.upsert_seller_profile(user["id"], endereco_retirada=valor)
            return await self._conv.build_reply("Endereço de retirada salvo!", {})

        if campo == "chave_pix":
            await user_repo.upsert_seller_profile(user["id"], chave_pix=valor)
            return await self._conv.build_reply("Chave Pix salva! Vou validar em breve.", {})

        return await self._conv.build_reply(f"Campo '{campo}' atualizado.", {})

    async def _handle_negotiation_response(
        self, phone, intent, user, neg, user_repo, neg_repo, listing_repo, engine
    ) -> str:
        aceitou = intent.get("aceitou", False)
        status = neg["status"]

        if status == "proposta_ao_vendedor":
            if aceitou:
                result = await engine.aceitar_proposta_vendedor(neg["id"])
                return await self._conv.build_reply(
                    f"Ótimo! Proposta de R${neg['preco_atual_proposto']:.2f} confirmada. Agora vou notificar o comprador.",
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

        return await self._conv.respond(
            phone=phone, user_message=intent.get("descricao", ""),
            history=_get_history(phone),
        )

    async def _handle_confirmation(
        self, phone, text, pending, user, user_repo, listing_repo,
        neg_repo, tx_repo, delivery_repo, engine,
    ) -> str:
        intent = await self._conv.extract_intent(text, contexto="confirmacao")
        aceitou = intent.get("aceitou", False)
        tipo = pending.get("tipo")

        if tipo == "confirmar_preco_listing":
            PENDING_CONFIRMATIONS.pop(phone, None)
            if not aceitou:
                return await self._conv.build_reply(
                    "Sem problema! Me diga o preço que você prefere anunciar.", {}
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
                f"Produto anunciado com sucesso! ID #{listing['id']}. "
                f"Preço: R${appraisal['preco_sugerido']:.2f}. "
                "Avisarei quando houver interessados!",
                {"listing_id": listing["id"]},
            )

        PENDING_CONFIRMATIONS.pop(phone, None)
        return await self._conv.build_reply("Ok, ação cancelada.", {})

    def _build_context_string(self, user, active_negs) -> str:
        parts = []
        if user:
            parts.append(f"Usuário: {user.get('nome', 'sem nome')} (id={user['id']})")
        if active_negs:
            parts.append(f"Negociações ativas: {len(active_negs)}")
        return " | ".join(parts) if parts else "novo usuário"

    def _summarize_negs(self, negs) -> str:
        if not negs:
            return "sem negociação ativa"
        statuses = [n["status"] for n in negs]
        return f"{len(negs)} negociação(ões) ativa(s): {', '.join(statuses)}"

    async def reset(self, phone: str) -> None:
        _clear_history(phone)

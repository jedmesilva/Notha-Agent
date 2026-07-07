"""
Investor Profile Tools — ferramentas do agente para gestão de perfil de investidor
e resposta a ofertas de investimento.

Tools:
  - configurar_perfil_investidor : cria/atualiza preferências de investimento
  - consultar_perfil_investidor  : exibe o perfil atual e métricas
  - responder_oferta_investimento: aceita, modifica ou recusa uma oferta pendente
  - listar_ofertas_pendentes     : lista ofertas aguardando resposta do investidor
"""
import logging
from decimal import Decimal, InvalidOperation
from tools.base import Tool

logger = logging.getLogger("notha.tools.investor_profile")


def _fmt_brl(v) -> str:
    try:
        return f"R$ {Decimal(str(v)):,.2f}"
    except Exception:
        return str(v)


def _to_decimal(v, default=None):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────

class ConfigurarPerfilInvestidor(Tool):
    name = "configurar_perfil_investidor"
    description = (
        "Cria ou atualiza o perfil de preferências de investimento do usuário. "
        "Use quando o investidor quiser definir: tolerância a risco, valor mínimo/máximo "
        "por investimento, prazo preferido, nível preferido ou ativar investimento automático. "
        "Também use para registrar um novo investidor na plataforma."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "ID do usuário investidor.",
            },
            "risk_tolerance": {
                "type": "string",
                "enum": ["conservative", "moderate", "aggressive"],
                "description": (
                    "Tolerância a risco: 'conservative' (baixo risco, menor retorno), "
                    "'moderate' (equilíbrio), 'aggressive' (maior risco, maior retorno)."
                ),
            },
            "min_investment_amount": {
                "type": "number",
                "description": "Valor mínimo por investimento em BRL (ex: 50).",
            },
            "max_investment_amount": {
                "type": "number",
                "description": (
                    "Valor máximo por investimento em BRL. "
                    "Omitir = sem limite além do saldo da carteira."
                ),
            },
            "min_term_days": {
                "type": "integer",
                "description": "Prazo mínimo aceito em dias (ex: 30 = 1 mês).",
            },
            "max_term_days": {
                "type": "integer",
                "description": "Prazo máximo aceito em dias (ex: 365 = 1 ano).",
            },
            "auto_invest": {
                "type": "boolean",
                "description": (
                    "Se true, investe automaticamente quando surgir uma oportunidade "
                    "compatível, sem precisar confirmar no WhatsApp. "
                    "Padrão: false (recebe notificação e decide)."
                ),
            },
            "level_id": {
                "type": "integer",
                "description": (
                    "ID do nível (fundo) de preferência (1–10). "
                    "Omitir = aceita qualquer nível."
                ),
            },
        },
        "required": ["user_id"],
    }

    async def execute(
        self,
        user_id: int,
        risk_tolerance: str = "moderate",
        min_investment_amount: float = 50.0,
        max_investment_amount: float | None = None,
        min_term_days: int = 1,
        max_term_days: int = 365,
        auto_invest: bool = False,
        level_id: int | None = None,
    ) -> str:
        from db.connection import get_db
        from db.repositories.investor_profiles import InvestorProfileRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        if risk_tolerance not in ("conservative", "moderate", "aggressive"):
            return "❌ risk_tolerance inválido. Use: conservative, moderate ou aggressive."
        if min_term_days > max_term_days:
            return "❌ min_term_days não pode ser maior que max_term_days."

        min_amt = Decimal(str(min_investment_amount))
        max_amt = Decimal(str(max_investment_amount)) if max_investment_amount else None

        try:
            repo       = InvestorProfileRepository(db)
            profile_id = await repo.upsert(
                user_id=user_id,
                level_id=level_id,
                risk_tolerance=risk_tolerance,
                min_investment_amount=min_amt,
                max_investment_amount=max_amt,
                min_term_days=min_term_days,
                max_term_days=max_term_days,
                auto_invest=auto_invest,
            )

            risk_labels = {
                "conservative": "Conservador 🟢",
                "moderate":     "Moderado 🟡",
                "aggressive":   "Arrojado 🔴",
            }
            auto_label  = "✅ Ativo (investe automaticamente)" if auto_invest else "❌ Manual (você confirma cada oferta)"
            max_label   = _fmt_brl(max_amt) if max_amt else "Sem limite (usa saldo disponível)"
            level_label = f"Nível {level_id}" if level_id else "Qualquer nível"

            return (
                f"✅ *Perfil de investidor salvo!*\n\n"
                f"  Risco: {risk_labels[risk_tolerance]}\n"
                f"  Valor mínimo por investimento: {_fmt_brl(min_amt)}\n"
                f"  Valor máximo por investimento: {max_label}\n"
                f"  Prazo aceito: {min_term_days} a {max_term_days} dias\n"
                f"  Nível preferido: {level_label}\n"
                f"  Investimento automático: {auto_label}\n\n"
                f"Quando surgir uma oportunidade compatível, você será notificado "
                f"{'e o investimento será realizado automaticamente' if auto_invest else 'pelo WhatsApp'}. 🎯"
            )
        except Exception as e:
            logger.error("configurar_perfil_investidor user=%d: %s", user_id, e)
            return f"❌ Erro ao salvar perfil: {e}"


class ConsultarPerfilInvestidor(Tool):
    name = "consultar_perfil_investidor"
    description = (
        "Exibe o perfil de investidor do usuário: preferências configuradas e "
        "métricas históricas (valor médio, prazo médio, total investido). "
        "Use quando o investidor perguntar sobre seu perfil ou histórico."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "ID do usuário investidor."},
        },
        "required": ["user_id"],
    }

    async def execute(self, user_id: int) -> str:
        from db.connection import get_db
        from db.repositories.investor_profiles import InvestorProfileRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            profile = await InvestorProfileRepository(db).get_by_user(user_id)
            if not profile:
                return (
                    "Você ainda não tem um perfil de investidor configurado. 📋\n\n"
                    "Use *configurar_perfil_investidor* para definir suas preferências "
                    "e começar a receber oportunidades de investimento."
                )

            risk_labels = {
                "conservative": "Conservador 🟢",
                "moderate":     "Moderado 🟡",
                "aggressive":   "Arrojado 🔴",
            }
            auto_label   = "✅ Automático" if profile["auto_invest"] else "✋ Manual"
            max_label    = _fmt_brl(profile["max_investment_amount"]) if profile["max_investment_amount"] else "Sem limite"
            level_label  = f"Nível {profile['level_id']}" if profile["level_id"] else "Qualquer nível"
            status       = "✅ Ativo" if profile["is_active"] else "⏸️ Pausado"

            lines = [
                f"📋 *Seu perfil de investidor:*\n",
                f"  Status: {status}",
                f"  Risco: {risk_labels.get(profile['risk_tolerance'], profile['risk_tolerance'])}",
                f"  Valor por investimento: {_fmt_brl(profile['min_investment_amount'])} → {max_label}",
                f"  Prazo aceito: {profile['min_term_days']} a {profile['max_term_days']} dias",
                f"  Nível preferido: {level_label}",
                f"  Modo: {auto_label}",
            ]

            if profile.get("last_metrics_at"):
                lines.append(f"\n📊 *Histórico:*")
                if profile.get("avg_investment_amount"):
                    lines.append(f"  Valor médio investido: {_fmt_brl(profile['avg_investment_amount'])}")
                if profile.get("avg_term_days"):
                    lines.append(f"  Prazo médio: {profile['avg_term_days']} dias")
                lines.append(f"  Total investido (acumulado): {_fmt_brl(profile['total_invested_lifetime'])}")
                lines.append(f"  Investimentos ativos: {profile['active_investment_count']}")

            return "\n".join(lines)
        except Exception as e:
            logger.error("consultar_perfil_investidor user=%d: %s", user_id, e)
            return f"❌ Erro ao consultar perfil: {e}"


class ListarOfertasPendentes(Tool):
    name = "listar_ofertas_pendentes"
    description = (
        "Lista as ofertas de investimento pendentes (aguardando confirmação) do usuário. "
        "Use quando o investidor perguntar sobre ofertas recebidas, oportunidades "
        "enviadas para ele, ou quando quiser ver o que tem para confirmar."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "ID do usuário investidor."},
        },
        "required": ["user_id"],
    }

    async def execute(self, user_id: int) -> str:
        from db.connection import get_db

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            offers = await db.fetch_all(
                """
                SELECT io.*, o.expected_rate, lv.name AS level_name
                FROM investment_offers io
                JOIN investment_opportunities o ON o.id = io.opportunity_id
                JOIN levels lv ON lv.id = io.level_id
                WHERE io.user_id = $1
                  AND io.status = 'pending'
                  AND io.expires_at > NOW()
                ORDER BY io.created_at DESC
                """,
                user_id,
            )

            if not offers:
                return (
                    "Você não tem ofertas de investimento pendentes no momento. 📭\n\n"
                    "Quando uma nova oportunidade compatível com seu perfil surgir, "
                    "você receberá uma notificação aqui."
                )

            lines = [f"📬 *Suas ofertas pendentes ({len(offers)}):*\n"]
            for o in offers:
                rate_pct = float(o["expected_rate"]) * 100
                mat = o["maturity_at"].strftime("%d/%m/%Y") if o.get("maturity_at") else "—"
                exp = o["expires_at"].strftime("%d/%m %H:%M") if o.get("expires_at") else "—"
                lines.append(
                    f"🔹 *Oferta #{o['id']}* — {o['level_name']}\n"
                    f"  Valor sugerido: {_fmt_brl(o['suggested_amount'])}\n"
                    f"  Taxa: {rate_pct:.2f}% a.a. | Vencimento: {mat}\n"
                    f"  Expira: {exp}"
                )

            lines.append(
                "\n💡 Para responder: diga *confirmar oferta #ID*, "
                "*alterar oferta #ID para R$200* ou *recusar oferta #ID*."
            )
            return "\n\n".join(lines)
        except Exception as e:
            logger.error("listar_ofertas_pendentes user=%d: %s", user_id, e)
            return f"❌ Erro ao listar ofertas: {e}"


class ResponderOfertaInvestimento(Tool):
    name = "responder_oferta_investimento"
    description = (
        "Processa a resposta de um investidor a uma oferta de investimento pendente. "
        "Use quando o investidor confirmar, modificar o valor ou recusar uma oferta. "
        "Palavras-chave: 'confirmar', 'aceitar', 'alterar', 'recusar', 'declinar'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "ID do usuário investidor.",
            },
            "offer_id": {
                "type": "integer",
                "description": "ID da oferta (aparece na mensagem original, ex: 'oferta #42').",
            },
            "action": {
                "type": "string",
                "enum": ["accept", "modify", "decline"],
                "description": (
                    "'accept'  — confirma o valor sugerido na oferta.\n"
                    "'modify'  — investe um valor diferente (informar custom_amount).\n"
                    "'decline' — recusa a oferta."
                ),
            },
            "custom_amount": {
                "type": "number",
                "description": (
                    "Novo valor de investimento em BRL. "
                    "Obrigatório somente quando action='modify'."
                ),
            },
        },
        "required": ["user_id", "offer_id", "action"],
    }

    async def execute(
        self,
        user_id: int,
        offer_id: int,
        action: str,
        custom_amount: float | None = None,
    ) -> str:
        from db.connection import get_db
        from engine.investor_matching import process_offer_response

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        if action not in ("accept", "modify", "decline"):
            return "❌ action inválido. Use: accept, modify ou decline."
        if action == "modify" and custom_amount is None:
            return "❌ Informe o valor (custom_amount) ao usar action='modify'."

        custom_dec = _to_decimal(custom_amount) if custom_amount else None

        try:
            result = await process_offer_response(
                db=db,
                offer_id=offer_id,
                user_id=user_id,
                action=action,
                custom_amount=custom_dec,
            )

            if not result.get("ok"):
                return f"❌ {result.get('error', 'Erro desconhecido.')}"

            if result["action"] == "declined":
                return (
                    f"✅ Oferta #{offer_id} recusada.\n\n"
                    f"Você continuará recebendo novas oportunidades compatíveis com seu perfil. 📊"
                )

            inv_id   = result["investment_id"]
            amount   = result["final_amount"]
            interest = result["interest_at_maturity"]
            mat      = str(result.get("maturity_at", ""))[:10]

            return (
                f"✅ *Investimento confirmado!*\n\n"
                f"  Investimento #{inv_id}\n"
                f"  Valor: {_fmt_brl(amount)}\n"
                f"  Juros no vencimento: {_fmt_brl(interest)}\n"
                f"  Vencimento: {mat}\n\n"
                f"No vencimento, principal + juros serão creditados automaticamente na sua carteira. 🎉\n"
                f"Use *consultar_investimentos* para acompanhar."
            )
        except Exception as e:
            logger.error("responder_oferta user=%d offer=%d: %s", user_id, offer_id, e)
            return f"❌ Erro ao processar resposta: {e}"

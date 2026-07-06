"""
Investment Tools — ferramentas do agente conversacional para investidores.

Tools:
  - listar_oportunidades   : exibe oportunidades de investimento abertas
  - investir               : investidor aceita uma oportunidade
  - consultar_investimentos: posição consolidada do investidor
"""
import logging
from decimal import Decimal, InvalidOperation
from tools.base import Tool

logger = logging.getLogger("notha.tools.investment")


def _fmt_brl(value) -> str:
    try:
        return f"R$ {Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


def _fmt_rate(value) -> str:
    try:
        return f"{float(value) * 100:.2f}% a.m."
    except Exception:
        return str(value)


def _to_decimal(value, default=Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────

class ListarOportunidades(Tool):
    name = "listar_oportunidades"
    description = (
        "Lista as oportunidades de investimento abertas no fundo. "
        "Use quando o investidor perguntar onde pode investir, "
        "quais oportunidades existem, ou quiser ver o que está disponível."
    )
    parameters = {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "integer",
                "description": "ID do grupo (fundo). Se não souber, omita para ver todos.",
            },
            "limit": {
                "type": "integer",
                "description": "Máximo de oportunidades a exibir (padrão: 5)",
                "default": 5,
            },
        },
        "required": [],
    }

    async def execute(self, group_id: int | None = None, limit: int = 5) -> str:
        from db.connection import get_db
        from db.repositories.opportunities import OpportunityRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            opp_repo = OpportunityRepository(db)
            opps = await opp_repo.list_open(group_id=group_id, limit=limit)

            if not opps:
                return (
                    "Não há oportunidades de investimento abertas no momento. "
                    "Quando um empréstimo for aprovado, uma nova oportunidade será gerada automaticamente. 📭"
                )

            lines = [f"📈 *Oportunidades de investimento ({len(opps)} disponíveis):*\n"]
            for o in opps:
                needed    = Decimal(str(o["amount_needed"]))
                committed = Decimal(str(o["amount_committed"]))
                remaining = needed - committed
                pct       = int((committed / needed * 100)) if needed > 0 else 0

                debt_ref = f" | Empréstimo #{o['debt_id']}" if o.get("debt_id") else ""
                expires  = o["expires_at"].strftime("%d/%m/%Y") if o.get("expires_at") else "—"

                lines.append(
                    f"🔹 *Oportunidade #{o['id']}* — {o.get('group_name', 'Grupo')}{debt_ref}\n"
                    f"  Captação total: {_fmt_brl(needed)}\n"
                    f"  Já captado: {_fmt_brl(committed)} ({pct}%)\n"
                    f"  *Ainda disponível: {_fmt_brl(remaining)}*\n"
                    f"  Taxa: {_fmt_rate(o['expected_rate'])}\n"
                    f"  Expira: {expires}"
                )

            lines.append(
                "\n💡 Para investir, diga: \"Quero investir R$ X na oportunidade #ID\""
            )
            return "\n\n".join(lines)
        except Exception as e:
            logger.error("listar_oportunidades error: %s", e)
            return f"❌ Erro ao listar oportunidades: {e}"


class InvestirTool(Tool):
    name = "investir"
    description = (
        "Registra o investimento de um usuário em uma oportunidade de captação do fundo. "
        "Debita o valor da wallet do investidor e credita no fundo. "
        "Use quando o investidor confirmar que quer investir um valor específico em uma oportunidade. "
        "Sempre pergunte o prazo/vencimento desejado antes de chamar esta tool — é obrigatório."
    )
    parameters = {
        "type": "object",
        "properties": {
            "investor_user_id": {
                "type": "integer",
                "description": "ID do usuário investidor",
            },
            "opportunity_id": {
                "type": "integer",
                "description": "ID da oportunidade de investimento",
            },
            "amount": {
                "type": "number",
                "description": "Valor a investir em BRL",
            },
            "maturity_at": {
                "type": "string",
                "description": (
                    "Vencimento do investimento em ISO-8601. Pode ser curto (minutos, horas) "
                    "ou longo (dias, meses). Exemplos: '2025-03-01T10:00:00Z', '2025-06-30', "
                    "'2025-01-01T00:30:00Z'. Sempre pergunte ao investidor o prazo desejado."
                ),
            },
        },
        "required": ["investor_user_id", "opportunity_id", "amount", "maturity_at"],
    }

    async def execute(
        self,
        investor_user_id: int,
        opportunity_id: int,
        amount: float,
        maturity_at: str,
    ) -> str:
        from db.connection import get_db
        from engine.investment_engine import accept_investment
        from datetime import datetime, timezone

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        inv_amount = _to_decimal(amount)
        if inv_amount <= Decimal("0"):
            return "❌ Valor de investimento inválido."

        # Parse maturity_at
        try:
            mat_str = maturity_at.strip().replace(" ", "T")
            if "T" in mat_str:
                mat_dt = datetime.fromisoformat(mat_str.replace("Z", "+00:00"))
            else:
                from datetime import date as _date, time as _time
                d = _date.fromisoformat(mat_str)
                mat_dt = datetime.combine(d, _time.max, tzinfo=timezone.utc)
        except ValueError as exc:
            return f"❌ Vencimento inválido — use formato ISO-8601 (ex: '2025-12-31T23:59:00Z'): {exc}"

        try:
            result = await accept_investment(
                db=db,
                opportunity_id=opportunity_id,
                investor_user_id=investor_user_id,
                amount=inv_amount,
                maturity_at=mat_dt,
            )

            if not result.get("ok"):
                return f"❌ {result.get('error', 'Erro desconhecido')}"

            inv_id      = result["investment_id"]
            rate        = result["rate_agreed"]
            interest    = result["interest_at_maturity"]
            maturity    = result["maturity_at"]
            opp_status  = result["new_opportunity_status"]

            status_msg = {
                "fully_funded":     "✅ Oportunidade 100% financiada! O fundo foi reabastecido.",
                "partially_funded": "🔄 Oportunidade parcialmente financiada.",
                "open":             "📭 Oportunidade ainda aberta para outros investidores.",
            }.get(opp_status, "")

            return (
                f"✅ *Investimento confirmado!*\n\n"
                f"  Investimento #{inv_id}\n"
                f"  Valor investido: {_fmt_brl(inv_amount)}\n"
                f"  Taxa acordada: {_fmt_rate(rate)}\n"
                f"  Juros no vencimento: {_fmt_brl(interest)}\n"
                f"  Vencimento: {maturity[:10]}\n\n"
                f"{status_msg}\n\n"
                f"No vencimento, você receberá o principal + juros automaticamente na sua carteira. "
                f"Use *consultar_investimentos* para acompanhar sua posição. 📊"
            )
        except Exception as e:
            logger.error("investir error: %s", e)
            return f"❌ Erro ao registrar investimento: {e}"


class ConsultarInvestimentos(Tool):
    name = "consultar_investimentos"
    description = (
        "Consulta a posição consolidada do investidor: valor investido, "
        "rendimento acumulado, pagamentos pendentes e oportunidades abertas. "
        "Use quando o investidor perguntar sobre seus investimentos, rendimentos ou saldo."
    )
    parameters = {
        "type": "object",
        "properties": {
            "investor_user_id": {
                "type": "integer",
                "description": "ID do usuário investidor",
            },
            "group_id": {
                "type": "integer",
                "description": "ID do grupo (fundo)",
            },
        },
        "required": ["investor_user_id", "group_id"],
    }

    async def execute(self, investor_user_id: int, group_id: int) -> str:
        from db.connection import get_db
        from engine.investment_engine import get_investor_summary

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            summary = await get_investor_summary(db, investor_user_id, group_id)

            pos     = summary["position"]
            balance = summary["wallet_balance"]
            active  = summary["active_investments"]
            opps    = summary["open_opportunities"]

            lines = [
                f"📊 *Sua posição de investidor:*\n",
                f"  💼 Saldo disponível na carteira: {_fmt_brl(balance)}",
                f"  📈 Total investido (ativo): {_fmt_brl(pos['total_invested'])}",
                f"  💹 Total recebido em rendimentos: {_fmt_brl(pos['total_returned'])}",
                f"  ⏳ Rendimentos agendados: {_fmt_brl(pos['pending_payouts'])}",
                f"  🔢 Investimentos ativos: {pos['active_investments']}",
            ]

            if active:
                lines.append("\n*Investimentos ativos:*")
                for inv in active[:5]:
                    lines.append(
                        f"  • #{inv['id']} — {_fmt_brl(inv['amount_invested'])} "
                        f"@ {_fmt_rate(inv['rate_agreed'])} "
                        f"({inv.get('group_name', 'Grupo')})"
                    )

            if opps:
                lines.append(f"\n📭 *{len(opps)} oportunidade(s) abertas* no fundo.")
                lines.append("  Diga \"listar oportunidades\" para ver detalhes.")

            return "\n".join(lines)
        except Exception as e:
            logger.error("consultar_investimentos error: %s", e)
            return f"❌ Erro ao consultar investimentos: {e}"

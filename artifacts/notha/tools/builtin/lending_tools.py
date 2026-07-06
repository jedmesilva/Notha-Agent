"""
Lending Tools — ferramentas financeiras expostas ao agente conversacional.

Cada tool recebe parâmetros do LLM e executa via LendingEngine/RateEngine.
O LLM nunca toca em valores financeiros direto — só chama estas funções.

Tools disponíveis:
  - solicitar_emprestimo    : cria loan_request + proposed_installments
  - consultar_extrato       : saldo e extrato da wallet do usuário
  - consultar_dividas       : lista dívidas e parcelas do usuário
  - registrar_pagamento     : registra um pagamento e aloca via FIFO
  - consultar_limite        : mostra limite de crédito disponível
  - calcular_cotacao_taxa   : mostra a taxa estimada antes de formalizar
"""
import json
import logging
from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
from tools.base import Tool

logger = logging.getLogger("notha.tools.lending")


def _to_decimal(value, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return default


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


# ─────────────────────────────────────────────────────────────────────────────

class SolicitarEmprestimoTool(Tool):
    name = "solicitar_emprestimo"
    description = (
        "Cria uma solicitação de empréstimo para o usuário. "
        "Use quando o usuário confirmar que quer pedir empréstimo, "
        "informando o valor, o grupo e o plano de parcelas. "
        "Não confirma a aprovação — apenas registra a proposta."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id":          {"type": "integer", "description": "ID do usuário solicitante"},
            "group_id":         {"type": "integer", "description": "ID do grupo credor"},
            "requested_amount": {"type": "number",  "description": "Valor solicitado em BRL"},
            "num_installments": {"type": "integer", "description": "Número de parcelas mensais"},
            "first_due_days":   {
                "type": "integer",
                "description": "Dias até o vencimento da 1ª parcela (padrão: 30)",
                "default": 30,
            },
        },
        "required": ["user_id", "group_id", "requested_amount", "num_installments"],
    }

    async def execute(
        self,
        user_id: int,
        group_id: int,
        requested_amount: float,
        num_installments: int,
        first_due_days: int = 30,
    ) -> str:
        from db.connection import get_db
        from db.repositories.loans import LoanRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível no momento."

        amount = _to_decimal(requested_amount)
        if amount <= Decimal("0"):
            return "❌ Valor inválido para o empréstimo."
        if num_installments < 1 or num_installments > 60:
            return "❌ Número de parcelas deve ser entre 1 e 60."

        loan_repo = LoanRepository(db)

        try:
            request_id = await loan_repo.create_request(
                user_id=user_id,
                group_id=group_id,
                requested_amount=amount,
            )

            # Parcelas iguais distribuídas mensalmente
            installment_amount = (amount / Decimal(str(num_installments))).quantize(
                Decimal("0.01")
            )
            # Ajuste de centavos na última parcela para fechar o total exato
            remainder = amount - (installment_amount * Decimal(str(num_installments - 1)))

            installments = []
            for i in range(1, num_installments + 1):
                due = date.today() + timedelta(days=first_due_days + (i - 1) * 30)
                inst_amount = remainder if i == num_installments else installment_amount
                installments.append({
                    "sequence":          i,
                    "proposed_due_date": due,
                    "proposed_amount":   inst_amount,
                    "distribution_type": "equal",
                })
            await loan_repo.add_proposed_installments_bulk(request_id, installments)

            first_due = date.today() + timedelta(days=first_due_days)
            return (
                f"✅ Solicitação #{request_id} criada com sucesso!\n\n"
                f"• Valor: {_fmt_brl(amount)}\n"
                f"• Parcelas: {num_installments}x {_fmt_brl(installment_amount)}\n"
                f"• 1ª parcela: {first_due.strftime('%d/%m/%Y')}\n\n"
                f"Aguardando análise de crédito. "
                f"Posso verificar sua taxa estimada agora se quiser! 📊"
            )
        except Exception as e:
            logger.error("solicitar_emprestimo error: %s", e)
            return f"❌ Erro ao criar solicitação: {e}"


class ConsultarExtrato(Tool):
    name = "consultar_extrato"
    description = (
        "Consulta o saldo atual e as últimas transações da wallet do usuário. "
        "Use quando o usuário perguntar sobre saldo, extrato ou movimentações."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "ID do usuário"},
            "limit":   {"type": "integer", "description": "Número de transações a exibir (padrão: 5)", "default": 5},
        },
        "required": ["user_id"],
    }

    async def execute(self, user_id: int, limit: int = 5) -> str:
        from db.connection import get_db
        from db.repositories.wallets import WalletRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        wallet_repo = WalletRepository(db)
        try:
            wallet = await wallet_repo.get_by_owner("user", user_id)
            if not wallet:
                return "Você ainda não possui uma carteira ativa na plataforma."

            balance = await wallet_repo.true_balance(wallet["id"])
            txs = await wallet_repo.get_transactions(wallet["id"], limit=limit)

            type_labels = {
                "loan_disbursement":    "💰 Empréstimo recebido",
                "loan_repayment":       "💳 Pagamento de dívida",
                "investment_deposit":   "📈 Depósito de investimento",
                "investment_withdrawal":"📤 Saque de investimento",
                "interest_payout":      "💹 Rendimento",
                "fee":                  "⚙️ Taxa",
                "adjustment":           "🔧 Ajuste",
            }

            lines = [f"💼 *Saldo atual:* {_fmt_brl(balance)}\n\n*Últimas movimentações:*"]
            for tx in txs:
                label = type_labels.get(tx["type"], tx["type"])
                signal = "+" if tx["amount"] >= 0 else ""
                lines.append(
                    f"  {label}: {signal}{_fmt_brl(tx['amount'])}"
                )

            if not txs:
                lines.append("  (nenhuma movimentação ainda)")

            return "\n".join(lines)
        except Exception as e:
            logger.error("consultar_extrato error: %s", e)
            return f"❌ Erro ao consultar extrato: {e}"


class ConsultarDividas(Tool):
    name = "consultar_dividas"
    description = (
        "Lista as dívidas ativas do usuário com status das parcelas. "
        "Use quando o usuário perguntar sobre empréstimos, dívidas, parcelas ou pagamentos em aberto."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "ID do usuário"},
        },
        "required": ["user_id"],
    }

    async def execute(self, user_id: int) -> str:
        from db.connection import get_db
        from engine.lending import get_user_debts_summary

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            summaries = await get_user_debts_summary(db, user_id)
            if not summaries:
                return "Você não tem empréstimos ativos no momento. 😊"

            lines = [f"📋 *Seus empréstimos ({len(summaries)} ativo(s)):*\n"]
            for s in summaries:
                status_emoji = {
                    "active": "🟡", "paid_off": "✅",
                    "defaulted": "🔴", "renegotiated": "🔵",
                }.get(s.get("status", ""), "⚪")

                next_due = s.get("next_due_date")
                next_due_str = next_due.strftime("%d/%m/%Y") if next_due else "—"

                lines.append(
                    f"{status_emoji} *Dívida #{s['debt_id']}*\n"
                    f"  Principal: {_fmt_brl(s['principal'])} | "
                    f"Taxa: {_fmt_rate(s['interest_rate'])}\n"
                    f"  Pago: {_fmt_brl(s['total_paid'])} | "
                    f"Restante: {_fmt_brl(s['total_remaining'])}\n"
                    f"  Parcelas: {s['open_installments']} em aberto"
                    + (f" ⚠️ {s['overdue_installments']} vencida(s)" if s.get("overdue_installments") else "")
                    + f"\n  Próximo vencimento: {next_due_str}"
                )

            return "\n\n".join(lines)
        except Exception as e:
            logger.error("consultar_dividas error: %s", e)
            return f"❌ Erro ao consultar dívidas: {e}"


class RegistrarPagamento(Tool):
    name = "registrar_pagamento"
    description = (
        "Registra um pagamento para uma dívida e aloca automaticamente entre as parcelas em aberto "
        "(FIFO — parcela mais antiga primeiro). "
        "Use quando o usuário confirmar que realizou um pagamento."
    )
    parameters = {
        "type": "object",
        "properties": {
            "debt_id":        {"type": "integer", "description": "ID da dívida a ser paga"},
            "amount_paid":    {"type": "number",  "description": "Valor pago em BRL"},
            "payment_method": {
                "type": "string",
                "description": "Método de pagamento (pix, boleto, transferencia)",
                "default": "pix",
            },
        },
        "required": ["debt_id", "amount_paid"],
    }

    async def execute(
        self,
        debt_id: int,
        amount_paid: float,
        payment_method: str = "pix",
    ) -> str:
        from db.connection import get_db
        from engine.lending import allocate_payment

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        amount = _to_decimal(amount_paid)
        if amount <= Decimal("0"):
            return "❌ Valor de pagamento inválido."

        try:
            result = await allocate_payment(
                db=db,
                debt_id=debt_id,
                amount_paid=amount,
                payment_method=payment_method,
            )

            if not result.get("ok"):
                return f"❌ Erro: {result.get('error')}"

            allocs = result.get("allocations", [])
            overpay = result.get("overpayment", Decimal("0"))
            fully_paid = result.get("fully_paid", False)

            lines = [
                f"✅ Pagamento de {_fmt_brl(amount)} registrado!\n",
                f"📑 *Parcelas quitadas/abatidas ({len(allocs)}):*",
            ]
            for a in allocs:
                status_icon = "✅" if a["installment_status"] == "paid" else "🔄"
                lines.append(
                    f"  {status_icon} Parcela #{a['sequence']}: "
                    f"{_fmt_brl(a['allocated'])} alocado"
                )

            if overpay > Decimal("0"):
                strategy = result.get("strategy_used", "")
                if strategy == "reduce_principal":
                    lines.append(f"\n💡 Excedente de {_fmt_brl(overpay)} abatido no principal.")
                else:
                    lines.append(f"\n💡 Excedente de {_fmt_brl(overpay)} registrado como pagamento antecipado.")

            if fully_paid:
                lines.append("\n🎉 *Parabéns! Sua dívida foi totalmente quitada!*")

            return "\n".join(lines)
        except Exception as e:
            logger.error("registrar_pagamento error: %s", e)
            return f"❌ Erro ao registrar pagamento: {e}"


class ConsultarLimite(Tool):
    name = "consultar_limite"
    description = (
        "Consulta o limite de crédito disponível do usuário em um grupo. "
        "Use quando o usuário perguntar quanto pode pegar emprestado."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id":  {"type": "integer", "description": "ID do usuário"},
            "group_id": {"type": "integer", "description": "ID do grupo credor"},
        },
        "required": ["user_id", "group_id"],
    }

    async def execute(self, user_id: int, group_id: int) -> str:
        from db.connection import get_db
        from db.repositories.credit_limits import CreditLimitRepository
        from db.repositories.loans import LoanRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            limit_repo = CreditLimitRepository(db)
            loan_repo  = LoanRepository(db)

            ind = await limit_repo.get_individual_limit("user", user_id, group_id)
            pool = await limit_repo.get_pool_limit(group_id)
            active_total = await loan_repo.active_debt_total(user_id, group_id)

            if not ind and not pool:
                return (
                    "Ainda não há limite de crédito configurado para você neste grupo. "
                    "Aguarde a análise de crédito ou contate o administrador."
                )

            lines = ["💳 *Seu limite de crédito:*\n"]
            if ind:
                limit = Decimal(str(ind["limit_amount"]))
                available = max(Decimal("0"), limit - active_total)
                lines.append(
                    f"  Limite individual: {_fmt_brl(limit)}\n"
                    f"  Em uso: {_fmt_brl(active_total)}\n"
                    f"  *Disponível: {_fmt_brl(available)}*"
                )

            if pool:
                max_exp = Decimal(str(pool["max_aggregate_exposure"]))
                cur_exp = Decimal(str(pool["current_exposure_cache"]))
                pool_available = max(Decimal("0"), max_exp - cur_exp)
                lines.append(
                    f"\n  Teto do grupo: {_fmt_brl(max_exp)}\n"
                    f"  Exposição atual: {_fmt_brl(cur_exp)}\n"
                    f"  Disponível no pool: {_fmt_brl(pool_available)}"
                )

            return "\n".join(lines)
        except Exception as e:
            logger.error("consultar_limite error: %s", e)
            return f"❌ Erro ao consultar limite: {e}"


class CalcularCotacaoTaxa(Tool):
    name = "calcular_cotacao_taxa"
    description = (
        "Calcula e exibe a taxa de juros estimada para um empréstimo, "
        "com base no perfil de risco do usuário e na liquidez do grupo. "
        "Use antes de formalizar a solicitação para mostrar ao usuário a taxa prevista."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id":    {"type": "integer", "description": "ID do usuário"},
            "group_id":   {"type": "integer", "description": "ID do grupo credor"},
            "amount":     {"type": "number",  "description": "Valor desejado em BRL"},
            "term_days":  {"type": "integer", "description": "Prazo em dias"},
        },
        "required": ["user_id", "group_id", "amount", "term_days"],
    }

    async def execute(
        self,
        user_id: int,
        group_id: int,
        amount: float,
        term_days: int,
    ) -> str:
        from db.connection import get_db
        from engine.scoring_engine import get_or_compute_score
        from db.repositories.rates import RateRepository

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            score = await get_or_compute_score(db, user_id)
            rate_repo = RateRepository(db)
            policy = await rate_repo.get_active_policy(group_id)

            if not policy:
                return "Sem política de taxa configurada para este grupo. Contate o administrador."

            from engine.rate_engine import (
                _compute_risk_premium, _compute_liquidity_multiplier
            )
            from decimal import Decimal

            base_rate    = Decimal(str(policy["base_borrowing_rate"]))
            min_spread   = Decimal(str(policy["min_spread"]))
            adj_bps      = await rate_repo.get_term_adjustment(group_id, term_days)
            term_adj     = Decimal(str(adj_bps)) / Decimal("10000")
            risk_premium = _compute_risk_premium(score)

            liquidity = await rate_repo.get_latest_liquidity(group_id)
            if liquidity:
                demand = Decimal(str(liquidity["total_active_loan_demand"]))
                supply = Decimal(str(liquidity["total_available_investment"]))
            else:
                demand, supply = Decimal("1"), Decimal("1")

            liq_mult   = _compute_liquidity_multiplier(demand, supply)
            final_rate = ((base_rate + risk_premium + term_adj) * liq_mult).quantize(
                Decimal("0.000001")
            )

            # Custo total estimado
            total_interest = (Decimal(str(amount)) * final_rate * Decimal(str(term_days)) / Decimal("30"))
            total_cost = Decimal(str(amount)) + total_interest

            return (
                f"📊 *Cotação estimada:*\n\n"
                f"  Valor: {_fmt_brl(amount)}\n"
                f"  Prazo: {term_days} dias\n"
                f"  Taxa: {_fmt_rate(final_rate)}\n\n"
                f"  Score de crédito: {float(score):.0f}/1000\n"
                f"  Taxa base: {_fmt_rate(base_rate)}\n"
                f"  Prêmio de risco: {_fmt_rate(risk_premium)}\n"
                f"  Ajuste de prazo: {adj_bps} bps\n"
                f"  Multiplicador de liquidez: {float(liq_mult):.2f}x\n\n"
                f"  *Custo total estimado: {_fmt_brl(total_cost)}*\n"
                f"  _(juros estimados: {_fmt_brl(total_interest)})_\n\n"
                f"⚠️ Cotação válida por 24h. A taxa final é congelada no momento da aprovação."
            )
        except Exception as e:
            logger.error("calcular_cotacao_taxa error: %s", e)
            return f"❌ Erro ao calcular cotação: {e}"


class AprovarEmprestimoTool(Tool):
    name = "aprovar_emprestimo"
    description = (
        "Aprova uma solicitação de empréstimo pendente: valida limites, "
        "congela a taxa, cria a dívida e as parcelas, e movimenta as wallets. "
        "Use apenas após confirmação do usuário/admin de que a aprovação deve ser feita."
    )
    parameters = {
        "type": "object",
        "properties": {
            "loan_request_id": {"type": "integer", "description": "ID da solicitação de empréstimo"},
            "approved_by":     {"type": "string",  "description": "Quem aprovou (usuário admin ou 'system')"},
        },
        "required": ["loan_request_id"],
    }

    async def execute(
        self, loan_request_id: int, approved_by: str = "system"
    ) -> str:
        from db.connection import get_db
        from engine.lending import approve_loan

        db = get_db()
        if not db:
            return "❌ Banco de dados indisponível."

        try:
            result = await approve_loan(db, loan_request_id, approved_by=approved_by)

            if not result.get("ok"):
                return (
                    f"❌ Solicitação #{loan_request_id} *reprovada*.\n\n"
                    f"Motivo: {result.get('rejection_reason', 'não especificado')}"
                )

            debt_id   = result["debt_id"]
            rate      = result["final_rate"]
            term_days = result["term_days"]
            n_insts   = result["installments"]

            return (
                f"✅ *Empréstimo aprovado!*\n\n"
                f"  Solicitação: #{loan_request_id}\n"
                f"  Dívida criada: #{debt_id}\n"
                f"  Taxa: {_fmt_rate(rate)}\n"
                f"  Prazo: {term_days} dias\n"
                f"  Parcelas geradas: {n_insts}\n\n"
                f"O valor foi creditado na carteira do usuário. "
                f"Use *consultar_dividas* para ver o plano de pagamento completo."
            )
        except Exception as e:
            logger.error("aprovar_emprestimo error: %s", e)
            return f"❌ Erro ao aprovar empréstimo: {e}"

"""
InvestmentEngine — lógica de captação e distribuição de rendimentos.

Fluxo principal:
  1. create_opportunity()   → gerada automaticamente após approve_loan()
  2. accept_investment()    → investidor compromete dinheiro, wallet_tx entra no fundo
  3. distribute_payouts()   → job mensal: calcula e paga rendimento proporcional
  4. get_investor_summary() → posição consolidada do investidor

Princípio: LLM propõe, código executa. Nenhum valor financeiro é calculado pelo LLM.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta, datetime, timezone

logger = logging.getLogger("notha.investment_engine")

_ZERO = Decimal("0")

# Prazo padrão de validade de uma oportunidade (dias)
DEFAULT_OPPORTUNITY_TTL_DAYS = 30


# ── 1. Criar oportunidade de captação ─────────────────────────────────────────

async def create_opportunity(
    db,
    group_id: int,
    amount_needed: Decimal,
    debt_id: int | None = None,
    ttl_days: int = DEFAULT_OPPORTUNITY_TTL_DAYS,
) -> dict:
    """
    Cria uma investment_opportunity para repor o saldo retirado do fundo.
    Chamada automaticamente ao final de approve_loan().

    Retorna: {opportunity_id, amount_needed, expected_rate, expires_at}
    """
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.rates import RateRepository

    opp_repo  = OpportunityRepository(db)
    rate_repo = RateRepository(db)

    # Snapshot da taxa de investimento do grupo no momento da criação
    policy = await rate_repo.get_active_policy(group_id)
    expected_rate = (
        Decimal(str(policy["base_investment_rate"])) if policy else Decimal("0.02")
    )

    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

    opp_id = await opp_repo.create(
        group_id=group_id,
        amount_needed=amount_needed,
        expected_rate=expected_rate,
        expires_at=expires_at,
        debt_id=debt_id,
    )

    logger.info(
        "Oportunidade de investimento criada: id=%d group=%d amount=R$%.2f debt=%s",
        opp_id, group_id, float(amount_needed), debt_id,
    )

    return {
        "opportunity_id": opp_id,
        "amount_needed":  amount_needed,
        "expected_rate":  expected_rate,
        "expires_at":     expires_at,
    }


# ── 2. Investidor aceita uma oportunidade ─────────────────────────────────────

async def accept_investment(
    db,
    opportunity_id: int,
    investor_user_id: int,
    amount: Decimal,
    maturity_date: date | None = None,
) -> dict:
    """
    Registra o investimento de um usuário em uma oportunidade.

    Ações:
      1. Valida oportunidade (aberta, não expirada, amount <= remaining)
      2. Debita wallet do investidor
      3. Credita wallet do grupo (fundo reabastecido)
      4. Cria registro em investments
      5. Atualiza amount_committed na oportunidade
      6. Agenda payout mensal proporcional

    Retorna: {ok, investment_id, new_opportunity_status}
    """
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.investments import InvestmentRepository
    from db.repositories.wallets import WalletRepository

    opp_repo    = OpportunityRepository(db)
    inv_repo    = InvestmentRepository(db)
    wallet_repo = WalletRepository(db)

    # Valida oportunidade
    opp = await opp_repo.get_by_id(opportunity_id)
    if not opp:
        return {"ok": False, "error": "Oportunidade não encontrada"}
    if opp["status"] not in ("open", "partially_funded"):
        return {"ok": False, "error": f"Oportunidade não está aberta (status={opp['status']})"}
    if opp["expires_at"] < datetime.now(timezone.utc):
        await opp_repo.expire_stale()
        return {"ok": False, "error": "Oportunidade expirada"}

    remaining = Decimal(str(opp["amount_needed"])) - Decimal(str(opp["amount_committed"]))
    if amount > remaining:
        return {
            "ok":    False,
            "error": f"Valor excede o restante da oportunidade: disponível R$ {remaining:.2f}",
        }

    group_id    = opp["group_id"]
    rate_agreed = Decimal(str(opp["expected_rate"]))

    # Wallets
    investor_wallet = await wallet_repo.get_or_create("user",  investor_user_id)
    group_wallet    = await wallet_repo.get_or_create("group", group_id)

    # Verifica saldo do investidor
    investor_balance = await wallet_repo.true_balance(investor_wallet["id"])
    if investor_balance < amount:
        return {
            "ok":    False,
            "error": f"Saldo insuficiente: disponível R$ {investor_balance:.2f}, solicitado R$ {amount:.2f}",
        }

    # Registra investimento
    inv_id = await inv_repo.create(
        investor_user_id=investor_user_id,
        group_id=group_id,
        amount_invested=amount,
        rate_agreed=rate_agreed,
        opportunity_id=opportunity_id,
        maturity_date=maturity_date,
    )

    # Wallet transactions
    ref = str(inv_id)
    await wallet_repo.add_transaction(
        wallet_id=investor_wallet["id"],
        amount=-amount,
        tx_type="investment_deposit",
        reference_id=ref,
        reference_type="investment",
        description=f"Investimento no fundo — oportunidade #{opportunity_id}",
    )
    await wallet_repo.add_transaction(
        wallet_id=group_wallet["id"],
        amount=amount,
        tx_type="investment_deposit",
        reference_id=ref,
        reference_type="investment",
        description=f"Captação de investimento #{inv_id}",
    )

    # Atualiza oportunidade
    new_status = await opp_repo.add_commitment(opportunity_id, amount)

    # Agenda payout mensal (primeiro payout em 30 dias, proporcional à taxa)
    monthly_return = (amount * rate_agreed).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    today = date.today()
    await inv_repo.schedule_payout(
        investment_id=inv_id,
        amount=monthly_return,
        period_start=today,
        period_end=today + timedelta(days=30),
        scheduled_date=today + timedelta(days=30),
    )

    logger.info(
        "Investimento aceito: inv_id=%d opp=%d investor=%d amount=R$%.2f rate=%.4f%% opp_status=%s",
        inv_id, opportunity_id, investor_user_id,
        float(amount), float(rate_agreed) * 100, new_status,
    )

    return {
        "ok":                    True,
        "investment_id":         inv_id,
        "amount_invested":       amount,
        "rate_agreed":           rate_agreed,
        "monthly_return":        monthly_return,
        "new_opportunity_status": new_status,
    }


# ── 3. Distribuir payouts vencidos ────────────────────────────────────────────

async def distribute_payouts(db) -> dict:
    """
    Processa todos os investment_payouts com scheduled_date <= hoje.

    Para cada payout:
      1. Debita wallet do grupo (interest_payout)
      2. Credita wallet do investidor (interest_payout)
      3. Marca payout como 'paid'
      4. Agenda próximo payout mensal (se investimento ainda ativo)

    Retorna: {paid_count, total_distributed, errors}
    """
    from db.repositories.investments import InvestmentRepository
    from db.repositories.wallets import WalletRepository

    inv_repo    = InvestmentRepository(db)
    wallet_repo = WalletRepository(db)

    payouts = await inv_repo.list_pending_payouts(up_to_date=date.today())

    paid_count       = 0
    total_distributed = _ZERO
    errors: list[str] = []

    for payout in payouts:
        payout_id     = payout["id"]
        inv_id        = payout["investment_id"]
        investor_id   = payout["investor_user_id"]
        group_id      = payout["group_id"]
        amount        = Decimal(str(payout["amount"]))

        try:
            group_wallet    = await wallet_repo.get_or_create("group", group_id)
            investor_wallet = await wallet_repo.get_or_create("user",  investor_id)

            # Verifica saldo do grupo para pagar
            group_balance = await wallet_repo.true_balance(group_wallet["id"])
            if group_balance < amount:
                errors.append(
                    f"Payout #{payout_id} ignorado: saldo do grupo insuficiente "
                    f"(R$ {group_balance:.2f} < R$ {amount:.2f})"
                )
                continue

            ref = str(payout_id)
            await wallet_repo.add_transaction(
                wallet_id=group_wallet["id"],
                amount=-amount,
                tx_type="interest_payout",
                reference_id=ref,
                reference_type="investment_payout",
                description=f"Rendimento pago ao investidor — inv #{inv_id}",
            )
            await wallet_repo.add_transaction(
                wallet_id=investor_wallet["id"],
                amount=amount,
                tx_type="interest_payout",
                reference_id=ref,
                reference_type="investment_payout",
                description=f"Rendimento recebido — investimento #{inv_id}",
            )

            await inv_repo.mark_payout_paid(payout_id)

            # Agenda próximo payout (se investimento ainda ativo)
            inv = await inv_repo.get_by_id(inv_id)
            if inv and inv["status"] == "active":
                period_end  = payout["period_end"]
                next_start  = period_end
                next_end    = next_start + timedelta(days=30)
                next_due    = next_start + timedelta(days=30)

                # Não agenda além do maturity_date
                if not inv["maturity_date"] or next_due <= inv["maturity_date"]:
                    await inv_repo.schedule_payout(
                        investment_id=inv_id,
                        amount=amount,
                        period_start=next_start,
                        period_end=next_end,
                        scheduled_date=next_due,
                    )

            paid_count        += 1
            total_distributed += amount

        except Exception as e:
            errors.append(f"Payout #{payout_id}: {e}")
            logger.error("distribute_payouts payout_id=%d: %s", payout_id, e)

    logger.info(
        "distribute_payouts: %d pago(s), total=R$%.2f, erros=%d",
        paid_count, float(total_distributed), len(errors),
    )

    return {
        "paid_count":        paid_count,
        "total_distributed": total_distributed,
        "errors":            errors,
    }


# ── 4. Visão do investidor ────────────────────────────────────────────────────

async def get_investor_summary(db, investor_user_id: int, group_id: int) -> dict:
    """Posição consolidada de um investidor em um grupo com oportunidades abertas."""
    from db.repositories.investments import InvestmentRepository
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.wallets import WalletRepository

    inv_repo    = InvestmentRepository(db)
    opp_repo    = OpportunityRepository(db)
    wallet_repo = WalletRepository(db)

    position     = await inv_repo.get_investor_position(investor_user_id, group_id)
    open_opps    = await opp_repo.list_open(group_id=group_id, limit=5)
    active_inv   = await inv_repo.list_by_investor(investor_user_id, status="active", limit=10)

    wallet = await wallet_repo.get_by_owner("user", investor_user_id)
    balance = await wallet_repo.true_balance(wallet["id"]) if wallet else _ZERO

    return {
        "wallet_balance":    balance,
        "position":          position,
        "active_investments": [dict(i) for i in active_inv],
        "open_opportunities": [dict(o) for o in open_opps],
    }

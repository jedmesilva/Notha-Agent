"""
InvestmentEngine — lógica de captação e distribuição de rendimentos.

Fluxo principal:
  1. create_opportunity()   → gerada automaticamente após approve_loan()
  2. accept_investment()    → investidor compromete dinheiro, wallet_tx entra no fundo
  3. distribute_payouts()   → job frequente: processa investimentos que venceram
  4. get_investor_summary() → posição consolidada do investidor

Modelo de prazo:
  Cada investimento tem um vencimento próprio definido em maturity_at (TIMESTAMPTZ).
  O prazo pode ser minutos, horas, dias, semanas ou meses — sem frequência fixa.
  No vencimento, o investidor recebe: juros (interest_payout) + principal (investment_withdrawal).
  Juros = principal × taxa_anual × fração_do_ano (juros simples).

Princípio: LLM propõe, código executa. Nenhum valor financeiro é calculado pelo LLM.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta, datetime, timezone, time as dt_time

logger = logging.getLogger("notha.investment_engine")

_ZERO = Decimal("0")

# Segundos num ano gregoriano (365.25 × 24 × 3600)
_YEAR_SECONDS = Decimal("31557600")

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
    maturity_date: date | None = None,   # deprecated — usar maturity_at
    maturity_at: datetime | None = None,  # vencimento preciso (pode ser minutos/horas)
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

    # ── Valida maturity_at ANTES de qualquer mutação no banco ────────────────
    # (evitar inconsistência financeira se o parâmetro for inválido)
    now_dt = datetime.now(timezone.utc)
    if maturity_at is None:
        return {
            "ok":    False,
            "error": (
                "maturity_at é obrigatório — informe o vencimento do investimento "
                "(pode ser minutos, horas, dias, semanas ou meses a partir de agora)."
            ),
        }
    if isinstance(maturity_at, date) and not isinstance(maturity_at, datetime):
        maturity_dt: datetime = datetime.combine(
            maturity_at, dt_time.max, tzinfo=timezone.utc
        )
    elif maturity_at.tzinfo is None:
        maturity_dt = maturity_at.replace(tzinfo=timezone.utc)
    else:
        maturity_dt = maturity_at

    if maturity_dt <= now_dt:
        return {
            "ok":    False,
            "error": "maturity_at deve ser uma data/hora futura.",
        }

    term_seconds = Decimal(str((maturity_dt - now_dt).total_seconds()))
    term_years   = term_seconds / _YEAR_SECONDS

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

    # Registra investimento — maturity_at tem prioridade; maturity_date como fallback
    inv_id = await inv_repo.create(
        investor_user_id=investor_user_id,
        group_id=group_id,
        amount_invested=amount,
        rate_agreed=rate_agreed,
        opportunity_id=opportunity_id,
        maturity_date=maturity_date,
        maturity_at=maturity_at,
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

    # ── Snapshot de liquidez em tempo real ────────────────────────────────────
    # O aporte do investidor aumenta o saldo do grupo — dispara snapshot pontual
    # para que as próximas cotações reflitam a nova liquidez imediatamente.
    try:
        import asyncio as _asyncio
        from engine.jobs import snapshot_liquidity_for_group
        _asyncio.create_task(snapshot_liquidity_for_group(db, group_id))
    except Exception:
        pass

    # ── Agenda payout único no vencimento ────────────────────────────────────
    # maturity_dt e term_years já foram computados na validação antecipada acima.
    # Juros simples: I = P × r × t   onde t = fração do ano gregoriano.
    interest_amount = (amount * rate_agreed * term_years).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    await inv_repo.schedule_payout(
        investment_id=inv_id,
        amount=interest_amount,
        period_start=now_dt.date(),
        period_end=maturity_dt.date(),
        scheduled_date=maturity_dt.date(),
        scheduled_at=maturity_dt,
    )

    logger.info(
        "Investimento aceito: inv_id=%d opp=%d investor=%d amount=R$%.2f "
        "rate=%.4f%% juros=R$%.2f vencimento=%s opp_status=%s",
        inv_id, opportunity_id, investor_user_id,
        float(amount), float(rate_agreed) * 100,
        float(interest_amount), maturity_dt.isoformat(), new_status,
    )

    return {
        "ok":                    True,
        "investment_id":         inv_id,
        "amount_invested":       amount,
        "rate_agreed":           rate_agreed,
        "interest_at_maturity":  interest_amount,
        "maturity_at":           maturity_dt.isoformat(),
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
        payout_id   = payout["id"]
        inv_id      = payout["investment_id"]
        investor_id = payout["investor_user_id"]
        group_id    = payout["group_id"]
        interest    = Decimal(str(payout["amount"]))

        try:
            inv = await inv_repo.get_by_id(inv_id)
            if not inv or inv["status"] != "active":
                # Já processado ou cancelado; marca e segue
                await inv_repo.mark_payout_paid(payout_id)
                continue

            principal = Decimal(str(inv["amount_invested"]))
            total_due = principal + interest  # devolução de principal + juros

            group_wallet    = await wallet_repo.get_or_create("group", group_id)
            investor_wallet = await wallet_repo.get_or_create("user",  investor_id)

            # Verifica saldo do grupo (deve cobrir principal + juros)
            group_balance = await wallet_repo.true_balance(group_wallet["id"])
            if group_balance < total_due:
                errors.append(
                    f"Payout #{payout_id} ignorado: saldo do grupo insuficiente "
                    f"(R$ {group_balance:.2f} < juros R$ {interest:.2f} + "
                    f"principal R$ {principal:.2f} = R$ {total_due:.2f})"
                )
                continue

            ref = str(payout_id)

            # ── Bloco atômico: 4 movimentações + status ───────────────────
            # atomic() adquire uma única conexão + transação asyncpg.
            # Falha em qualquer passo reverte tudo — o payout permanece
            # 'scheduled' e será reprocessado sem dupla postagem.
            async with db.atomic() as tx:
                from db.repositories.wallets import WalletRepository as _WR
                from db.repositories.investments import InvestmentRepository as _IR
                wallet_tx = _WR(tx)
                inv_tx    = _IR(tx)

                # Juros: grupo → investidor
                await wallet_tx.add_transaction(
                    wallet_id=group_wallet["id"],
                    amount=-interest,
                    tx_type="interest_payout",
                    reference_id=ref,
                    reference_type="investment_payout",
                    description=f"Juros pagos ao investidor no vencimento — inv #{inv_id}",
                )
                await wallet_tx.add_transaction(
                    wallet_id=investor_wallet["id"],
                    amount=interest,
                    tx_type="interest_payout",
                    reference_id=ref,
                    reference_type="investment_payout",
                    description=f"Juros recebidos no vencimento — investimento #{inv_id}",
                )

                # Devolução de principal: grupo → investidor
                await wallet_tx.add_transaction(
                    wallet_id=group_wallet["id"],
                    amount=-principal,
                    tx_type="investment_withdrawal",
                    reference_id=ref,
                    reference_type="investment_payout",
                    description=f"Devolução de principal no vencimento — inv #{inv_id}",
                )
                await wallet_tx.add_transaction(
                    wallet_id=investor_wallet["id"],
                    amount=principal,
                    tx_type="investment_withdrawal",
                    reference_id=ref,
                    reference_type="investment_payout",
                    description=f"Principal devolvido no vencimento — investimento #{inv_id}",
                )

                await inv_tx.mark_payout_paid(payout_id)
                # Modelo de payout único: sem ciclo. Marca o investimento como 'matured'.
                await inv_tx.update_status(inv_id, "matured")

            paid_count        += 1
            total_distributed += total_due

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

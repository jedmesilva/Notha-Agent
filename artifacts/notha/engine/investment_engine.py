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
    level_id: int,
    amount_needed: Decimal,
    debt_id: int | None = None,
    ttl_days: int = DEFAULT_OPPORTUNITY_TTL_DAYS,
    loan_term_days: int | None = None,
) -> dict:
    """
    Cria uma investment_opportunity para repor o saldo retirado do pool do nível.
    Chamada automaticamente ao final de approve_loan().

    Retorna: {opportunity_id, amount_needed, expected_rate, expires_at}
    """
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.rates import RateRepository

    opp_repo  = OpportunityRepository(db)
    rate_repo = RateRepository(db)

    # Snapshot da taxa de investimento do nível no momento da criação
    policy = await rate_repo.get_active_policy(level_id)
    expected_rate = (
        Decimal(str(policy["base_investment_rate"])) if policy else Decimal("0.02")
    )

    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

    opp_id = await opp_repo.create(
        level_id=level_id,
        amount_needed=amount_needed,
        expected_rate=expected_rate,
        expires_at=expires_at,
        debt_id=debt_id,
    )

    # Vencimento real dos investimentos: prazo do empréstimo subjacente.
    # Se loan_term_days não for passado (criação manual), usa o TTL da oportunidade.
    investment_maturity_at = (
        datetime.now(timezone.utc) + timedelta(days=loan_term_days)
        if loan_term_days
        else expires_at
    )

    logger.info(
        "Oportunidade de investimento criada: id=%d level=%d amount=R$%.2f debt=%s maturity=%s",
        opp_id, level_id, float(amount_needed), debt_id,
        investment_maturity_at.isoformat(),
    )

    # ── Matching e notificação de investidores (fire-and-forget) ─────────────
    async def _run_matching() -> None:
        try:
            from engine.investor_matching import match_and_notify
            result = await match_and_notify(
                db=db,
                opportunity_id=opp_id,
                level_id=level_id,
                amount_needed=amount_needed,
                expected_rate=expected_rate,
                maturity_at=investment_maturity_at,
            )
            logger.info(
                "investor_matching opp=%d: alocado=R$%.2f (%.1f%%) auto=%d notificados=%d",
                opp_id, float(result["total_allocated"]), result["coverage_pct"],
                result["auto_invested"], result["notified"],
            )
        except Exception as exc:
            logger.error("investor_matching opp=%d: %s", opp_id, exc)

    import asyncio as _asyncio
    _asyncio.create_task(_run_matching())

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
      3. Credita wallet do nível (pool reabastecido)
      4. Cria registro em investments
      5. Atualiza amount_committed na oportunidade
      6. Agenda payout único no vencimento

    Retorna: {ok, investment_id, new_opportunity_status}
    """
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.investments import InvestmentRepository
    from db.repositories.wallets import WalletRepository

    # ── Valida maturity_at ANTES de qualquer mutação no banco ────────────────
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
        return {"ok": False, "error": "maturity_at deve ser uma data/hora futura."}

    term_seconds = Decimal(str((maturity_dt - now_dt).total_seconds()))
    term_years   = term_seconds / _YEAR_SECONDS

    # ── Garante que wallets existam ANTES da transação ────────────────────────
    _wallet_repo_pre = WalletRepository(db)
    investor_wallet = await _wallet_repo_pre.get_or_create("user", investor_user_id)
    level_wallet_id_holder: list[int] = []

    result_payload: dict = {}

    async with db.atomic() as tx:
        opp_repo_tx    = OpportunityRepository(tx)
        inv_repo_tx    = InvestmentRepository(tx)
        wallet_repo_tx = WalletRepository(tx)

        # Lock de linha — impede aceitações concorrentes desta oportunidade
        opp = await tx.fetch_one(
            "SELECT * FROM investment_opportunities WHERE id = $1 FOR UPDATE",
            opportunity_id,
        )
        if not opp:
            result_payload = {"ok": False, "error": "Oportunidade não encontrada"}
        elif opp["status"] not in ("open", "partially_funded"):
            result_payload = {
                "ok":    False,
                "error": f"Oportunidade não está aberta (status={opp['status']})",
            }
        elif opp["expires_at"] < now_dt:
            result_payload = {"ok": False, "error": "Oportunidade expirada"}
        else:
            remaining = (
                Decimal(str(opp["amount_needed"])) - Decimal(str(opp["amount_committed"]))
            )
            if amount > remaining:
                result_payload = {
                    "ok":    False,
                    "error": f"Valor excede o restante da oportunidade: disponível R$ {remaining:.2f}",
                }
            else:
                level_id    = opp["level_id"]
                rate_agreed = Decimal(str(opp["expected_rate"]))
                opp_debt_id = opp["debt_id"] if opp["debt_id"] else None

                # Garante wallet do nível dentro da transação
                level_wallet = await wallet_repo_tx.get_or_create("level", level_id)
                level_wallet_id_holder.append(level_wallet["id"])

                # Verifica saldo atual dentro da transação (leitura consistente)
                investor_balance = await wallet_repo_tx.true_balance(investor_wallet["id"])
                if investor_balance < amount:
                    result_payload = {
                        "ok":    False,
                        "error": (
                            f"Saldo insuficiente: disponível R$ {investor_balance:.2f}, "
                            f"solicitado R$ {amount:.2f}"
                        ),
                    }
                else:
                    inv_id = await inv_repo_tx.create(
                        investor_user_id=investor_user_id,
                        level_id=level_id,
                        amount_invested=amount,
                        rate_agreed=rate_agreed,
                        opportunity_id=opportunity_id,
                        debt_id=opp_debt_id,
                        maturity_date=maturity_date,
                        maturity_at=maturity_at,
                    )

                    ref = str(inv_id)
                    await wallet_repo_tx.add_transaction(
                        wallet_id=investor_wallet["id"],
                        amount=-amount,
                        tx_type="investment_deposit",
                        reference_id=ref,
                        reference_type="investment",
                        description=f"Investimento no fundo nível {level_id} — oportunidade #{opportunity_id}",
                    )
                    await wallet_repo_tx.add_transaction(
                        wallet_id=level_wallet["id"],
                        amount=amount,
                        tx_type="investment_deposit",
                        reference_id=ref,
                        reference_type="investment",
                        description=f"Captação de investimento #{inv_id}",
                    )

                    new_status = await opp_repo_tx.add_commitment(opportunity_id, amount)

                    # Juros simples: I = P × r × t
                    interest_amount = (amount * rate_agreed * term_years).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    await inv_repo_tx.schedule_payout(
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

                    result_payload = {
                        "ok":                     True,
                        "investment_id":          inv_id,
                        "amount_invested":        amount,
                        "rate_agreed":            rate_agreed,
                        "interest_at_maturity":   interest_amount,
                        "maturity_at":            maturity_dt.isoformat(),
                        "new_opportunity_status": new_status,
                        "_level_id":              level_id,
                    }

    if not result_payload.get("ok"):
        return result_payload

    # ── Efeitos colaterais pós-commit (fire-and-forget) ───────────────────────
    try:
        import asyncio as _asyncio
        from engine.jobs import snapshot_liquidity_for_level
        _level_id = result_payload.pop("_level_id", None)
        if _level_id:
            _asyncio.create_task(snapshot_liquidity_for_level(db, _level_id))
    except Exception:
        pass

    return result_payload


# ── 3. Distribuir payouts vencidos ────────────────────────────────────────────

async def distribute_payouts(db) -> dict:
    """
    Processa todos os investment_payouts com scheduled_date <= hoje.

    Para cada payout:
      1. Debita wallet do nível (interest_payout)
      2. Credita wallet do investidor (interest_payout)
      3. Debita wallet do nível (devolução de principal)
      4. Credita wallet do investidor (devolução de principal)
      5. Marca payout como 'paid' e investimento como 'matured'

    Retorna: {paid_count, total_distributed, errors}
    """
    from db.repositories.investments import InvestmentRepository
    from db.repositories.wallets import WalletRepository

    inv_repo    = InvestmentRepository(db)
    wallet_repo = WalletRepository(db)

    payouts = await inv_repo.list_pending_payouts(up_to_date=date.today())

    paid_count        = 0
    total_distributed = _ZERO
    errors: list[str] = []

    for payout in payouts:
        payout_id   = payout["id"]
        inv_id      = payout["investment_id"]
        investor_id = payout["investor_user_id"]
        level_id    = payout["level_id"]
        interest    = Decimal(str(payout["amount"]))

        try:
            inv = await inv_repo.get_by_id(inv_id)
            if not inv or inv["status"] != "active":
                await inv_repo.mark_payout_paid(payout_id)
                continue

            principal = Decimal(str(inv["amount_invested"]))
            total_due = principal + interest

            level_wallet    = await wallet_repo.get_or_create("level", level_id)
            investor_wallet = await wallet_repo.get_or_create("user",  investor_id)

            group_balance = await wallet_repo.true_balance(level_wallet["id"])
            if group_balance < total_due:
                errors.append(
                    f"Payout #{payout_id} ignorado: saldo do nível {level_id} insuficiente "
                    f"(R$ {group_balance:.2f} < juros R$ {interest:.2f} + "
                    f"principal R$ {principal:.2f} = R$ {total_due:.2f})"
                )
                continue

            ref = str(payout_id)

            async with db.atomic() as tx:
                from db.repositories.wallets import WalletRepository as _WR
                from db.repositories.investments import InvestmentRepository as _IR
                wallet_tx = _WR(tx)
                inv_tx    = _IR(tx)

                # Juros: nível → investidor
                await wallet_tx.add_transaction(
                    wallet_id=level_wallet["id"],
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

                # Devolução de principal: nível → investidor
                await wallet_tx.add_transaction(
                    wallet_id=level_wallet["id"],
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

async def get_investor_summary(db, investor_user_id: int, level_id: int) -> dict:
    """Posição consolidada de um investidor em um nível com oportunidades abertas."""
    from db.repositories.investments import InvestmentRepository
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.wallets import WalletRepository

    inv_repo    = InvestmentRepository(db)
    opp_repo    = OpportunityRepository(db)
    wallet_repo = WalletRepository(db)

    position   = await inv_repo.get_investor_position(investor_user_id, level_id)
    open_opps  = await opp_repo.list_open(level_id=level_id, limit=5)
    active_inv = await inv_repo.list_by_investor(investor_user_id, status="active", limit=10)

    wallet  = await wallet_repo.get_by_owner("user", investor_user_id)
    balance = await wallet_repo.true_balance(wallet["id"]) if wallet else _ZERO

    return {
        "wallet_balance":     balance,
        "position":           position,
        "active_investments": [dict(i) for i in active_inv],
        "open_opportunities": [dict(o) for o in open_opps],
    }

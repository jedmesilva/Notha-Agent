"""
LendingEngine — núcleo financeiro da plataforma (seções 5, 6, 10 do documento).

Fluxos implementados:
  1. approve_loan        — valida limites → persiste debt → installments → wallet_txs
  2. allocate_payment    — distribui pagamento entre parcelas via FIFO
  3. get_loan_summary    — visão consolidada de uma dívida para o usuário

Princípio: LLM propõe, código financeiro executa deterministicamente.
Nenhum valor financeiro é decidido pelo LLM — apenas pelo código abaixo.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta

logger = logging.getLogger("notha.lending")

_ZERO = Decimal("0")


# ── 1. Aprovação de empréstimo ────────────────────────────────────────────────

async def approve_loan(
    db,
    loan_request_id: int,
    approved_by: str = "system",
) -> dict:
    """
    Fluxo completo de aprovação (seção 10, passos 2b–4):

      1. Valida limites (individual + pool)
      2. Obtém/calcula cotação de taxa
      3. Cria debts com interest_rate_applied congelado
      4. Gera debt_installments a partir de proposed_installments
      5. Registra wallet_transactions (débito grupo, crédito usuário)
      6. Atualiza group_pool_limits.current_exposure_cache

    Retorna dict com resultado: {ok, debt_id, final_rate, rejection_reason}
    """
    from db.repositories.loans import LoanRepository
    from db.repositories.debts import DebtRepository
    from db.repositories.wallets import WalletRepository
    from db.repositories.credit_limits import CreditLimitRepository
    from db.repositories.rates import RateRepository
    from engine.scoring_engine import get_or_compute_score
    from engine.rate_engine import compute_loan_quote

    loan_repo   = LoanRepository(db)
    debt_repo   = DebtRepository(db)
    wallet_repo = WalletRepository(db)
    limit_repo  = CreditLimitRepository(db)
    rate_repo   = RateRepository(db)

    # ── Carrega solicitação ───────────────────────────────────────────────────
    req = await loan_repo.get_by_id(loan_request_id)
    if not req:
        return {"ok": False, "rejection_reason": "Solicitação não encontrada"}
    if req["status"] != "pending":
        return {"ok": False, "rejection_reason": f"Solicitação já está '{req['status']}'"}

    user_id          = req["user_id"]
    group_id         = req["group_id"]
    requested_amount = Decimal(str(req["requested_amount"]))

    # ── Proposta de parcelas ──────────────────────────────────────────────────
    proposed = await loan_repo.get_proposed_installments(loan_request_id)
    if not proposed:
        return {"ok": False, "rejection_reason": "Sem parcelas propostas para esta solicitação"}

    total_proposed = sum(Decimal(str(p["proposed_amount"])) for p in proposed)
    term_days = (
        max(p["proposed_due_date"] for p in proposed) - date.today()
    ).days
    term_days = max(term_days, 1)

    # ── Validação de limites (seção 6) ────────────────────────────────────────
    active_debt_total = await loan_repo.active_debt_total(user_id, group_id)
    limits_ok, rejection_reason = await limit_repo.validate_limits(
        borrower_type="user",
        borrower_id=user_id,
        group_id=group_id,
        requested_amount=requested_amount,
        active_debt_total=active_debt_total,
    )
    if not limits_ok:
        await loan_repo.update_status(
            loan_request_id, "rejected",
            decided_by=approved_by,
            rejection_reason=rejection_reason,
        )
        logger.info("Loan request %d REJECTED: %s", loan_request_id, rejection_reason)
        return {"ok": False, "rejection_reason": rejection_reason}

    # ── Score de risco + cotação de taxa ──────────────────────────────────────
    user_risk_score = await get_or_compute_score(db, user_id)

    # Verifica se já existe cotação válida; se não, calcula
    existing_quote = await rate_repo.get_latest_loan_quote(loan_request_id)
    if existing_quote:
        final_rate = Decimal(str(existing_quote["final_rate"]))
        quote_id   = existing_quote["id"]
        logger.info("Usando cotação existente id=%d rate=%.4f%%", quote_id, float(final_rate)*100)
    else:
        quote_result = await compute_loan_quote(
            db=db,
            loan_request_id=loan_request_id,
            group_id=group_id,
            term_days=term_days,
            user_risk_score=user_risk_score,
        )
        final_rate = quote_result["final_rate"]
        quote_id   = quote_result["quote_id"]

    # ── Wallets ───────────────────────────────────────────────────────────────
    group_wallet = await wallet_repo.get_or_create("group", group_id)
    user_wallet  = await wallet_repo.get_or_create("user",  user_id)

    group_wallet_id = group_wallet["id"]
    user_wallet_id  = user_wallet["id"]

    # Verifica saldo do grupo (saldo real, não cache)
    group_true_balance = await wallet_repo.true_balance(group_wallet_id)
    if group_true_balance < requested_amount:
        reason = (
            f"Saldo insuficiente no grupo: disponível R$ {group_true_balance:.2f}, "
            f"solicitado R$ {requested_amount:.2f}"
        )
        await loan_repo.update_status(
            loan_request_id, "rejected",
            decided_by=approved_by,
            rejection_reason=reason,
        )
        return {"ok": False, "rejection_reason": reason}

    # ── Cria dívida (interest_rate_applied = snapshot imutável) ──────────────
    debt_id = await debt_repo.create(
        loan_request_id=loan_request_id,
        from_wallet_id=group_wallet_id,
        to_wallet_id=user_wallet_id,
        principal=requested_amount,
        interest_rate_applied=final_rate,
        term_days=term_days,
    )

    # ── Gera parcelas reais a partir da proposta ──────────────────────────────
    installments_data = [
        {
            "sequence":  p["sequence"],
            "due_date":  p["proposed_due_date"],
            "amount_due": Decimal(str(p["proposed_amount"])),
        }
        for p in proposed
    ]
    await debt_repo.add_installments_bulk(debt_id, installments_data)

    # ── Wallet transactions ───────────────────────────────────────────────────
    ref = str(loan_request_id)
    await wallet_repo.add_transaction(
        wallet_id=group_wallet_id,
        amount=-requested_amount,
        tx_type="loan_disbursement",
        reference_id=ref,
        reference_type="loan_request",
        description=f"Desembolso — solicitação #{loan_request_id}",
    )
    await wallet_repo.add_transaction(
        wallet_id=user_wallet_id,
        amount=requested_amount,
        tx_type="loan_disbursement",
        reference_id=ref,
        reference_type="loan_request",
        description=f"Empréstimo recebido — solicitação #{loan_request_id}",
    )

    # ── Atualiza exposição do pool ────────────────────────────────────────────
    from db.repositories.credit_limits import CreditLimitRepository
    await limit_repo.increment_exposure(group_id, requested_amount)

    # ── Aprova solicitação ────────────────────────────────────────────────────
    await loan_repo.update_status(
        loan_request_id, "approved", decided_by=approved_by
    )

    logger.info(
        "Loan APPROVED: request=%d debt=%d amount=R$%.2f rate=%.4f%% parcelas=%d",
        loan_request_id, debt_id, float(requested_amount),
        float(final_rate) * 100, len(proposed),
    )

    return {
        "ok":           True,
        "debt_id":      debt_id,
        "final_rate":   final_rate,
        "term_days":    term_days,
        "installments": len(proposed),
        "quote_id":     quote_id,
    }


# ── 2. Alocação de pagamentos (FIFO) ─────────────────────────────────────────

async def allocate_payment(
    db,
    debt_id: int,
    amount_paid: Decimal,
    payment_method: str = "pix",
    asaas_charge_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """
    Registra um pagamento e distribui entre as parcelas em aberto (FIFO).

    Algoritmo (seção 5 do documento):
      1. Cria payments record
      2. Busca parcelas abertas ordenadas por due_date ASC (mais antiga primeiro)
      3. Para cada parcela, aloca o mínimo entre (remaining_amount, saldo restante)
      4. Cria payment_allocations para cada alocação
      5. Atualiza installment.remaining_amount e status via DebtRepository
      6. Se sobrar valor após quitar todas as parcelas: aplica overpayment_strategy
         advance_installments → crédito antecipado na próxima parcela
         reduce_principal     → abate no principal da dívida
      7. Verifica se a dívida está totalmente quitada → atualiza status para 'paid_off'
      8. Registra wallet_transaction de repagamento

    Retorna dict com resumo das alocações.
    """
    from db.repositories.debts import DebtRepository
    from db.repositories.payments import PaymentRepository
    from db.repositories.wallets import WalletRepository
    from db.repositories.loans import LoanRepository

    debt_repo    = DebtRepository(db)
    payment_repo = PaymentRepository(db)
    wallet_repo  = WalletRepository(db)
    loan_repo    = LoanRepository(db)

    # Verifica dívida
    debt = await debt_repo.get_by_id(debt_id)
    if not debt:
        return {"ok": False, "error": "Dívida não encontrada"}
    if debt["status"] in ("paid_off", "renegotiated"):
        return {"ok": False, "error": f"Dívida já está '{debt['status']}'"}

    overpayment_strategy = debt["overpayment_strategy"]

    # Cria o registro de pagamento
    payment_id = await payment_repo.create(
        debt_id=debt_id,
        amount_paid=amount_paid,
        payment_method=payment_method,
        asaas_charge_id=asaas_charge_id,
        notes=notes,
    )

    # Carrega parcelas em aberto (FIFO)
    open_installments = await debt_repo.list_open_installments(debt_id)

    remaining_to_allocate = amount_paid
    allocations: list[dict] = []

    for inst in open_installments:
        if remaining_to_allocate <= _ZERO:
            break

        inst_id    = inst["id"]
        remaining  = Decimal(str(inst["remaining_amount"]))
        to_allocate = min(remaining, remaining_to_allocate)

        alloc_id = await payment_repo.add_allocation(
            payment_id=payment_id,
            installment_id=inst_id,
            amount_allocated=to_allocate,
        )
        new_status = await debt_repo.apply_allocation(inst_id, to_allocate)

        remaining_to_allocate -= to_allocate
        allocations.append({
            "allocation_id":  alloc_id,
            "installment_id": inst_id,
            "sequence":       inst["sequence"],
            "allocated":      to_allocate,
            "installment_status": new_status,
        })

    # ── Tratamento de overpayment ─────────────────────────────────────────────
    overpayment = remaining_to_allocate
    if overpayment > _ZERO:
        if overpayment_strategy == "reduce_principal":
            await debt_repo.reduce_principal(debt_id, overpayment)
            logger.info(
                "Overpayment R$%.2f aplicado como abate de principal (debt=%d)",
                float(overpayment), debt_id,
            )
        else:
            # advance_installments: o excedente fica como crédito (não implementa
            # nova parcela automática — apenas loga; o gestor decide manualmente)
            logger.info(
                "Overpayment R$%.2f registrado como pagamento antecipado (debt=%d) — "
                "sem parcelas futuras abertas para alocar.",
                float(overpayment), debt_id,
            )

    # ── Verifica quitação total ───────────────────────────────────────────────
    fully_paid = await debt_repo.check_debt_fully_paid(debt_id)
    if fully_paid:
        await debt_repo.update_status(debt_id, "paid_off")
        # Decrementa exposição do pool
        req = await loan_repo.get_by_id(debt["loan_request_id"])
        if req:
            from db.repositories.credit_limits import CreditLimitRepository
            limit_repo = CreditLimitRepository(db)
            await limit_repo.decrement_exposure(
                req["group_id"], Decimal(str(debt["principal"]))
            )
        logger.info("Dívida %d totalmente quitada.", debt_id)

    # ── Wallet transaction de repagamento ─────────────────────────────────────
    user_wallet  = await wallet_repo.get_by_id(debt["to_wallet_id"])
    group_wallet = await wallet_repo.get_by_id(debt["from_wallet_id"])

    if user_wallet:
        await wallet_repo.add_transaction(
            wallet_id=user_wallet["id"],
            amount=-amount_paid,
            tx_type="loan_repayment",
            reference_id=str(payment_id),
            reference_type="payment",
            description=f"Pagamento da dívida #{debt_id}",
        )
    if group_wallet:
        await wallet_repo.add_transaction(
            wallet_id=group_wallet["id"],
            amount=amount_paid,
            tx_type="loan_repayment",
            reference_id=str(payment_id),
            reference_type="payment",
            description=f"Recebimento da dívida #{debt_id}",
        )

    logger.info(
        "Payment id=%d debt=%d amount=R$%.2f allocations=%d overpayment=R$%.2f fully_paid=%s",
        payment_id, debt_id, float(amount_paid),
        len(allocations), float(overpayment), fully_paid,
    )

    return {
        "ok":           True,
        "payment_id":   payment_id,
        "amount_paid":  amount_paid,
        "allocations":  allocations,
        "overpayment":  overpayment,
        "fully_paid":   fully_paid,
        "strategy_used": overpayment_strategy if overpayment > _ZERO else None,
    }


# ── 3. Visão resumida de dívida para o usuário ────────────────────────────────

async def get_loan_summary(db, debt_id: int) -> dict:
    """Retorna visão consolidada de uma dívida para exibir ao usuário."""
    from db.repositories.debts import DebtRepository
    from db.repositories.payments import PaymentRepository
    from db.repositories.loans import LoanRepository

    debt_repo    = DebtRepository(db)
    payment_repo = PaymentRepository(db)
    loan_repo    = LoanRepository(db)

    debt = await debt_repo.get_by_id(debt_id)
    if not debt:
        return {}

    req   = await loan_repo.get_by_id(debt["loan_request_id"])
    insts = await debt_repo.list_all_installments(debt_id)
    total_paid = await payment_repo.total_paid(debt_id)

    open_insts   = [i for i in insts if i["status"] != "paid"]
    overdue_insts = [i for i in insts if i["status"] == "overdue"]
    total_remaining = sum(Decimal(str(i["remaining_amount"])) for i in open_insts)
    next_due = min(
        (i["due_date"] for i in open_insts), default=None
    )

    return {
        "debt_id":              debt_id,
        "loan_request_id":      debt["loan_request_id"],
        "user_id":              req["user_id"] if req else None,
        "group_id":             req["group_id"] if req else None,
        "principal":            Decimal(str(debt["principal"])),
        "interest_rate":        Decimal(str(debt["interest_rate_applied"])),
        "term_days":            debt["term_days"],
        "status":               debt["status"],
        "total_installments":   len(insts),
        "open_installments":    len(open_insts),
        "overdue_installments": len(overdue_insts),
        "total_paid":           total_paid,
        "total_remaining":      total_remaining,
        "next_due_date":        next_due,
        "disbursed_at":         debt["disbursed_at"],
    }


async def get_user_debts_summary(db, user_id: int) -> list[dict]:
    """Lista todas as dívidas ativas de um usuário com resumo."""
    from db.repositories.loans import LoanRepository
    from db.repositories.debts import DebtRepository

    loan_repo = LoanRepository(db)
    debt_repo = DebtRepository(db)

    loan_reqs = await loan_repo.list_by_user(user_id, status="approved", limit=20)
    summaries = []
    for req in loan_reqs:
        debt = await debt_repo.get_by_loan_request(req["id"])
        if debt:
            summary = await get_loan_summary(db, debt["id"])
            summaries.append(summary)
    return summaries

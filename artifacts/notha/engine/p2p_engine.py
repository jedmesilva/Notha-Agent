"""
P2PEngine — core financial logic for the P2P debt issuance flow.

Architecture: SEP (Sociedade de Empréstimo entre Pessoas) compliant.

Golden rule: NO credit instrument exists before real creditor capital is
committed. The platform is a pure intermediary — it never advances,
guarantees, or owns the credit.

Flow:
  1. submit_capture_request()   — borrower states intent (no capital committed)
  2. launch_capture_order()     — credit check + create open order for creditors
  3. commit_creditor_position() — creditor reserves funds (reserved status)
  4. confirm_creditor_position()— system confirms Pix receipt (confirmed status)
  5. check_and_close_order()    — if threshold met → emit_credit_instrument()
  6. emit_credit_instrument()   — create CreditInstrument + disburse to debtor
  7. process_debtor_payment()   — distribute payment to current creditors
  8. expire_stale_orders()      — job: revert reserved positions for expired orders

Principle: LLM proposes, code executes. No financial value is decided by LLM.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

logger = logging.getLogger("notha.p2p_engine")

_ZERO = Decimal("0")

# Default capture window: creditors have 72h to commit
DEFAULT_CAPTURE_WINDOW_HOURS = 72

# Default minimum threshold for capture viability (80%)
DEFAULT_MINIMUM_THRESHOLD_PCT = Decimal("0.80")

# Platform fees (defaults — override from level_policies in production)
DEFAULT_ORIGINATION_FEE_PCT = Decimal("0.02")   # 2% charged to debtor at disbursement
DEFAULT_SERVICING_FEE_PCT   = Decimal("0.005")  # 0.5% charged to creditor per passthrough


# ── 1. Submit capture request ──────────────────────────────────────────────────

async def submit_capture_request(
    db,
    *,
    user_id: int,
    level_id: int,
    requested_amount: Decimal,
    term_days: int,
    num_installments: int = 1,
    first_due_days: int = 30,
) -> dict:
    """
    Step 1: Borrower submits a capture request.

    Creates a CaptureRequest in 'draft' status. No capital is committed.
    Computes the credit score snapshot and builds the payment plan.

    Returns: {ok, capture_request_id, credit_score, payment_plan}
    """
    from db.repositories.capture_requests import CaptureRequestRepository
    from engine.scoring_engine import get_or_compute_score

    if requested_amount <= _ZERO:
        return {"ok": False, "rejection_reason": "requested_amount must be positive"}
    if term_days < 1:
        return {"ok": False, "rejection_reason": "term_days must be at least 1"}
    if num_installments < 1 or num_installments > 60:
        return {"ok": False, "rejection_reason": "num_installments must be between 1 and 60"}

    # Score snapshot at request time (informational — final check at launch)
    try:
        credit_score = await get_or_compute_score(db, user_id)
    except Exception as exc:
        logger.warning("Could not compute credit score for user %d: %s", user_id, exc)
        credit_score = None

    # Build payment plan (equal installments, monthly cadence)
    installment_amount = (
        requested_amount / Decimal(str(num_installments))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Adjust last installment for rounding
    remainder = requested_amount - installment_amount * Decimal(str(num_installments - 1))

    payment_plan = []
    for i in range(1, num_installments + 1):
        due = date.today() + timedelta(days=first_due_days + (i - 1) * 30)
        amt = remainder if i == num_installments else installment_amount
        payment_plan.append({
            "sequence":  i,
            "due_date":  due.isoformat(),
            "amount_due": str(amt),
        })

    repo = CaptureRequestRepository(db)
    request_id = await repo.create(
        user_id=user_id,
        level_id=level_id,
        requested_amount=requested_amount,
        term_days=term_days,
        payment_plan=payment_plan,
        credit_score_at_request=credit_score,
    )

    logger.info(
        "CaptureRequest created: id=%d user=%d level=%d amount=R$%.2f term=%dd score=%s",
        request_id, user_id, level_id, float(requested_amount), term_days, credit_score,
    )

    return {
        "ok":                 True,
        "capture_request_id": request_id,
        "credit_score":       float(credit_score) if credit_score else None,
        "payment_plan":       payment_plan,
    }


# ── 2. Launch capture order ────────────────────────────────────────────────────

async def launch_capture_order(
    db,
    capture_request_id: int,
    capture_window_hours: int = DEFAULT_CAPTURE_WINDOW_HOURS,
    minimum_threshold_pct: Decimal = DEFAULT_MINIMUM_THRESHOLD_PCT,
    origination_fee_pct: Decimal = DEFAULT_ORIGINATION_FEE_PCT,
    servicing_fee_pct: Decimal = DEFAULT_SERVICING_FEE_PCT,
    launched_by: str = "system",
) -> dict:
    """
    Step 2: Platform launches a CaptureOrder after credit assessment.

    Actions:
      1. Validates limits and credit constraints
      2. Computes interest rate (approved_rate for debtor, creditor_rate for creditors)
      3. Creates CaptureOrder in 'open' status
      4. Transitions CaptureRequest to 'in_capture'
      5. Triggers investor matching (fire-and-forget)

    Returns: {ok, capture_order_id, approved_rate, creditor_rate, capture_deadline}
    """
    from db.repositories.capture_requests import CaptureRequestRepository
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.credit_limits import CreditLimitRepository
    from db.repositories.loans import LoanRepository
    from db.repositories.rates import RateRepository
    from engine.scoring_engine import get_or_compute_score
    from engine.rate_engine import compute_loan_quote

    req_repo    = CaptureRequestRepository(db)
    order_repo  = CaptureOrderRepository(db)
    limit_repo  = CreditLimitRepository(db)
    loan_repo   = LoanRepository(db)
    rate_repo   = RateRepository(db)

    req = await req_repo.get_by_id(capture_request_id)
    if not req:
        return {"ok": False, "rejection_reason": "Capture request not found"}
    if req["status"] != "draft":
        return {"ok": False, "rejection_reason": f"Request already in status '{req['status']}'"}

    user_id          = req["user_id"]
    level_id         = req["level_id"]
    requested_amount = Decimal(str(req["requested_amount"]))
    term_days        = req["term_days"]

    # ── Credit score (authoritative — used for limit resolution) ──────────────
    credit_score = await get_or_compute_score(db, user_id)
    await req_repo.set_credit_score(capture_request_id, credit_score)

    # ── Validate credit limits ────────────────────────────────────────────────
    active_debt_total = await req_repo.active_debt_total(user_id)
    limits_ok, rejection_reason, limit_ctx = await limit_repo.validate_limits(
        borrower_type="user",
        borrower_id=user_id,
        level_id=level_id,
        requested_amount=requested_amount,
        active_debt_total=active_debt_total,
        user_risk_score=credit_score,
    )
    if not limits_ok:
        await req_repo.update_status(
            capture_request_id, "cancelled", rejection_reason=rejection_reason
        )
        logger.info("CaptureRequest %d CANCELLED: %s", capture_request_id, rejection_reason)
        return {"ok": False, "rejection_reason": rejection_reason}

    # ── Compute approved interest rate ────────────────────────────────────────
    # We create a temporary loan_request to reuse the existing rate engine
    # (which already computes base_rate + risk_premium + term_adj + liquidity_mult)
    tmp_loan_id = await loan_repo.create_request(
        user_id=user_id,
        level_id=level_id,
        requested_amount=requested_amount,
    )
    # Add placeholder installments (required by rate engine)
    payment_plan = req.get("payment_plan") or []
    if isinstance(payment_plan, str):
        payment_plan = json.loads(payment_plan)

    tmp_installments = [
        {
            "sequence":          p["sequence"],
            "proposed_due_date": date.fromisoformat(p["due_date"]) if isinstance(p["due_date"], str) else p["due_date"],
            "proposed_amount":   Decimal(str(p["amount_due"])),
            "distribution_type": "equal",
        }
        for p in payment_plan
    ]
    await loan_repo.add_proposed_installments_bulk(tmp_loan_id, tmp_installments)

    quote = await compute_loan_quote(
        db=db,
        loan_request_id=tmp_loan_id,
        level_id=level_id,
        term_days=term_days,
        user_risk_score=credit_score,
    )
    approved_rate = Decimal(str(quote["final_rate"]))

    # Clean up temporary loan request
    await db.execute("DELETE FROM loan_requests WHERE id = $1", tmp_loan_id)

    # creditor_rate = approved_rate minus origination spread
    # (origination fee is a one-off lump sum, not embedded in yield)
    # creditor_rate approximation: approved_rate adjusted for platform margin
    # Actual spread is the origination_fee_pct collected at disbursement.
    creditor_rate = approved_rate  # creditors receive the full approved rate

    # ── Minimum threshold amount ──────────────────────────────────────────────
    minimum_threshold = (requested_amount * minimum_threshold_pct).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    # ── Capture deadline ──────────────────────────────────────────────────────
    capture_deadline = datetime.now(timezone.utc) + timedelta(hours=capture_window_hours)

    # ── Create CaptureOrder ───────────────────────────────────────────────────
    order_id = await order_repo.create(
        capture_request_id=capture_request_id,
        target_amount=requested_amount,
        minimum_threshold=minimum_threshold,
        approved_rate=approved_rate,
        creditor_rate=creditor_rate,
        origination_fee_pct=origination_fee_pct,
        servicing_fee_pct=servicing_fee_pct,
        capture_deadline=capture_deadline,
    )

    # ── Transition capture request to in_capture ──────────────────────────────
    await req_repo.update_status(capture_request_id, "in_capture")

    logger.info(
        "CaptureOrder launched: order_id=%d request_id=%d amount=R$%.2f "
        "rate=%.4f%% threshold=R$%.2f deadline=%s",
        order_id, capture_request_id, float(requested_amount),
        float(approved_rate) * 100, float(minimum_threshold),
        capture_deadline.isoformat(),
    )

    # ── Trigger investor matching (fire-and-forget) ───────────────────────────
    async def _run_matching():
        try:
            from engine.investor_matching import match_and_notify
            from db.repositories.opportunities import OpportunityRepository

            # Create a compatible investment_opportunity record for the existing
            # matching engine to use (bridge between old and new systems)
            opp_repo = OpportunityRepository(db)
            opp_id = await opp_repo.create(
                level_id=level_id,
                amount_needed=requested_amount,
                expected_rate=creditor_rate,
                expires_at=capture_deadline,
                debt_id=None,
            )

            result = await match_and_notify(
                db=db,
                opportunity_id=opp_id,
                level_id=level_id,
                amount_needed=requested_amount,
                expected_rate=creditor_rate,
                maturity_at=capture_deadline + timedelta(days=term_days),
            )
            logger.info(
                "P2P investor matching order=%d: allocated=R$%.2f (%.1f%%) "
                "auto=%d notified=%d",
                order_id,
                float(result.get("total_allocated", 0)),
                result.get("coverage_pct", 0),
                result.get("auto_invested", 0),
                result.get("notified", 0),
            )
        except Exception as exc:
            logger.error("P2P investor matching order=%d: %s", order_id, exc)

    asyncio.create_task(_run_matching())

    return {
        "ok":               True,
        "capture_order_id": order_id,
        "approved_rate":    approved_rate,
        "creditor_rate":    creditor_rate,
        "minimum_threshold": minimum_threshold,
        "capture_deadline": capture_deadline.isoformat(),
        "origination_fee_pct": origination_fee_pct,
        "servicing_fee_pct":   servicing_fee_pct,
    }


# ── 3. Commit creditor position ────────────────────────────────────────────────

async def commit_creditor_position(
    db,
    *,
    capture_order_id: int,
    creditor_user_id: int,
    amount: Decimal,
    origin: str = "manual",
) -> dict:
    """
    Step 3: Creditor commits funds to a CaptureOrder.

    Creates a CreditorPosition in 'reserved' status and debits the creditor's
    wallet to an escrow (platform wallet). The position is NOT counted toward
    the capture total until confirmed (after Pix receipt).

    Returns: {ok, position_id, new_order_status}
    """
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.wallets import WalletRepository

    order_repo    = CaptureOrderRepository(db)
    position_repo = CreditorPositionRepository(db)
    wallet_repo   = WalletRepository(db)

    # ── Validate before any mutation ──────────────────────────────────────────
    now = datetime.now(timezone.utc)
    order = await order_repo.get_by_id(capture_order_id)
    if not order:
        return {"ok": False, "error": "Capture order not found"}
    if order["status"] != "open":
        return {"ok": False, "error": f"Order not open (status={order['status']})"}
    if order["capture_deadline"] < now:
        return {"ok": False, "error": "Capture order has expired"}

    remaining = Decimal(str(order["target_amount"])) - Decimal(str(order["committed_amount"]))
    if amount > remaining:
        return {"ok": False, "error": f"Amount exceeds remaining: available R$ {remaining:.2f}"}

    already_has = await position_repo.has_active_position(capture_order_id, creditor_user_id)
    if already_has:
        return {"ok": False, "error": "You already have an active position in this order"}

    # ── Atomic: debit creditor wallet → escrow + create position ─────────────
    creditor_wallet = await wallet_repo.get_or_create("user", creditor_user_id)
    platform_wallet = await wallet_repo.get_or_create("platform", 1)

    result_payload: dict = {}

    async with db.atomic() as tx:
        from db.repositories.wallets import WalletRepository as _WR
        from db.repositories.creditor_positions import CreditorPositionRepository as _PR

        wallet_tx    = _WR(tx)
        position_tx  = _PR(tx)

        # Lock creditor wallet row for consistent balance read
        creditor_balance = await wallet_tx.true_balance(creditor_wallet["id"])
        if creditor_balance < amount:
            result_payload = {
                "ok":    False,
                "error": f"Insufficient balance: available R$ {creditor_balance:.2f}, requested R$ {amount:.2f}",
            }
        else:
            position_id = await position_tx.create(
                capture_order_id=capture_order_id,
                creditor_user_id=creditor_user_id,
                committed_amount=amount,
                origin=origin,
            )
            ref = str(position_id)

            # Debit creditor wallet (funds held in escrow)
            await wallet_tx.add_transaction(
                wallet_id=creditor_wallet["id"],
                amount=-amount,
                tx_type="p2p_creditor_reserve",
                reference_id=ref,
                reference_type="creditor_position",
                description=f"P2P: funds reserved for capture order #{capture_order_id}",
            )
            # Credit platform escrow wallet
            await wallet_tx.add_transaction(
                wallet_id=platform_wallet["id"],
                amount=amount,
                tx_type="p2p_creditor_reserve",
                reference_id=ref,
                reference_type="creditor_position",
                description=f"P2P: creditor #{creditor_user_id} escrow for order #{capture_order_id}",
            )

            result_payload = {
                "ok":         True,
                "position_id": position_id,
            }

    if not result_payload.get("ok"):
        return result_payload

    logger.info(
        "CreditorPosition reserved: pos_id=%d order=%d creditor=%d amount=R$%.2f",
        result_payload["position_id"], capture_order_id, creditor_user_id, float(amount),
    )
    return result_payload


# ── 4. Confirm creditor position ───────────────────────────────────────────────

async def confirm_creditor_position(db, position_id: int) -> dict:
    """
    Step 4: System confirms a CreditorPosition after Pix receipt.

    Transitions reserved → confirmed and atomically increments the order's
    committed_amount so concurrent confirmations cannot double-count.

    Returns: {ok, new_order_status, instrument_id (if emitted)}
    """
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.capture_orders import CaptureOrderRepository

    # Pre-flight read (no lock needed — CAS guards the mutation below)
    position = await CreditorPositionRepository(db).get_by_id(position_id)
    if not position:
        return {"ok": False, "error": "Position not found"}
    if position["status"] != "reserved":
        return {"ok": False, "error": f"Position not in reserved status (status={position['status']})"}

    amount           = Decimal(str(position["committed_amount"]))
    capture_order_id = position["capture_order_id"]
    new_status: str  = "open"

    async with db.atomic() as tx:
        pos_tx   = CreditorPositionRepository(tx)
        order_tx = CaptureOrderRepository(tx)

        # CAS: confirm only if still 'reserved' (guards double-confirm)
        await pos_tx.confirm(position_id)

        # Atomic increment — only updates if order is still 'open'
        new_status = await order_tx.add_confirmed_commitment(capture_order_id, amount)

    logger.info(
        "CreditorPosition confirmed: pos_id=%d order=%d amount=R$%.2f order_status=%s",
        position_id, capture_order_id, float(amount), new_status,
    )

    instrument_id = None
    if new_status == "complete":
        result = await emit_credit_instrument(db, capture_order_id)
        if result.get("ok"):
            instrument_id = result["instrument_id"]
            logger.info(
                "CreditInstrument emitted automatically: id=%d order=%d",
                instrument_id, capture_order_id,
            )

    return {
        "ok":               True,
        "new_order_status": new_status,
        "instrument_id":    instrument_id,
    }


# ── 5. Check and close order (job-driven) ─────────────────────────────────────

async def check_and_close_order(db, capture_order_id: int) -> dict:
    """
    Evaluates whether a CaptureOrder should be closed:
      - If committed_amount >= target_amount: complete + emit instrument
      - If committed_amount >= minimum_threshold and deadline passed: complete + emit
      - If deadline passed and committed_amount < minimum_threshold: partial_expired + revert

    Returns: {ok, action, instrument_id}
    """
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.creditor_positions import CreditorPositionRepository

    order_repo    = CaptureOrderRepository(db)
    position_repo = CreditorPositionRepository(db)

    order = await order_repo.get_by_id(capture_order_id)
    if not order or order["status"] != "open":
        return {"ok": False, "error": "Order not found or not open"}

    now       = datetime.now(timezone.utc)
    committed = Decimal(str(order["committed_amount"]))
    target    = Decimal(str(order["target_amount"]))
    threshold = Decimal(str(order["minimum_threshold"]))
    expired   = order["capture_deadline"] < now

    # Fully funded: close immediately
    if committed >= target:
        result = await emit_credit_instrument(db, capture_order_id)
        return {"ok": True, "action": "complete", "instrument_id": result.get("instrument_id")}

    # Threshold met and deadline passed: close with partial funding
    if expired and committed >= threshold:
        result = await emit_credit_instrument(db, capture_order_id)
        return {"ok": True, "action": "partial_complete", "instrument_id": result.get("instrument_id")}

    # Expired without threshold: revert ALL positions (reserved and confirmed),
    # returning every creditor's capital from escrow.  Both statuses are refunded
    # because the order failed to reach minimum_threshold — no instrument is emitted.
    if expired and committed < threshold:
        reserved_positions  = await position_repo.list_by_order(capture_order_id, status="reserved")
        confirmed_positions = await position_repo.list_by_order(capture_order_id, status="confirmed")
        all_positions       = reserved_positions + confirmed_positions

        await order_repo.update_status(capture_order_id, "partial_expired")

        reverted = 0
        for pos in all_positions:
            await _revert_position(db, pos)
            reverted += 1

        # Update capture request status
        order_full = await order_repo.get_by_id(capture_order_id)
        if order_full:
            from db.repositories.capture_requests import CaptureRequestRepository
            req_repo = CaptureRequestRepository(db)
            await req_repo.update_status(order_full["capture_request_id"], "partial_expired")

        logger.info(
            "CaptureOrder %d expired without threshold: committed=R$%.2f threshold=R$%.2f "
            "reverted=%d (reserved=%d confirmed=%d)",
            capture_order_id, float(committed), float(threshold),
            reverted, len(reserved_positions), len(confirmed_positions),
        )
        return {"ok": True, "action": "partial_expired", "positions_reverted": reverted}

    return {"ok": True, "action": "no_action"}


async def _revert_position(db, position: dict) -> None:
    """Reverts a reserved position: refunds escrow → creditor wallet."""
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.wallets import WalletRepository

    position_repo = CreditorPositionRepository(db)
    wallet_repo   = WalletRepository(db)

    amount = Decimal(str(position["committed_amount"]))
    creditor_wallet = await wallet_repo.get_or_create("user", position["creditor_user_id"])
    platform_wallet = await wallet_repo.get_or_create("platform", 1)

    ref = str(position["id"])
    await wallet_repo.add_transaction(
        wallet_id=platform_wallet["id"],
        amount=-amount,
        tx_type="p2p_creditor_reserve_revert",
        reference_id=ref,
        reference_type="creditor_position",
        description=f"P2P: escrow refund for reverted position #{position['id']}",
    )
    await wallet_repo.add_transaction(
        wallet_id=creditor_wallet["id"],
        amount=amount,
        tx_type="p2p_creditor_reserve_revert",
        reference_id=ref,
        reference_type="creditor_position",
        description=f"P2P: funds returned — capture order did not complete",
    )
    await position_repo.revert(position["id"])


# ── 6. Emit credit instrument ──────────────────────────────────────────────────

async def emit_credit_instrument(db, capture_order_id: int) -> dict:
    """
    Step 6: Creates the CreditInstrument after CaptureOrder reaches 'complete'.

    ONLY callable when committed_amount >= minimum_threshold.

    All mutations execute inside a single db.atomic() transaction so a partial
    failure (e.g. DB error mid-way) leaves no orphaned instrument or disbursement.

    Fee accounting (conservation of funds):
      creditor escrow = total_committed_amount  (debited when positions reserved)
      At emission:
        platform_wallet -net_disbursed   → debtor_wallet +net_disbursed
        origination_fee stays in platform_wallet (implicit — never leaves escrow)
      No extra origination_fee credit is posted; doing so would double-count it.

    Returns: {ok, instrument_id, net_disbursed, origination_fee}
    """
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.capture_requests import CaptureRequestRepository
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.credit_instruments import CreditInstrumentRepository
    from db.repositories.wallets import WalletRepository

    # ── Pre-flight reads (no mutations yet) ───────────────────────────────────
    order = await CaptureOrderRepository(db).get_by_id(capture_order_id)
    if not order:
        return {"ok": False, "error": "Capture order not found"}
    if order["status"] not in ("open", "complete"):
        return {"ok": False, "error": f"Order in unexpected status: {order['status']}"}

    committed = Decimal(str(order["committed_amount"]))
    threshold = Decimal(str(order["minimum_threshold"]))
    if committed < threshold:
        return {
            "ok":    False,
            "error": f"Insufficient committed capital: R$ {committed:.2f} < threshold R$ {threshold:.2f}",
        }

    req = await CaptureRequestRepository(db).get_by_id(order["capture_request_id"])
    if not req:
        return {"ok": False, "error": "Capture request not found"}

    debtor_id = req["user_id"]

    # ── Fee calculation (deterministic, no DB) ────────────────────────────────
    origination_fee_pct = Decimal(str(order["origination_fee_pct"]))
    servicing_fee_pct   = Decimal(str(order["servicing_fee_pct"]))
    origination_fee = (committed * origination_fee_pct).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    net_disbursed = committed - origination_fee

    # ── Payment plan scaling (no DB) ─────────────────────────────────────────
    payment_plan = req.get("payment_plan") or []
    if isinstance(payment_plan, str):
        payment_plan = json.loads(payment_plan)

    requested_amount = Decimal(str(req["requested_amount"]))
    scale = (committed / requested_amount) if requested_amount > _ZERO else Decimal("1")
    scaled_plan = [
        {
            "sequence":   p["sequence"],
            "due_date":   p["due_date"],
            "amount_due": str(
                (Decimal(str(p["amount_due"])) * scale).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            ),
        }
        for p in payment_plan
    ]
    installment_data = [
        {
            "sequence":   p["sequence"],
            "due_date":   date.fromisoformat(p["due_date"]) if isinstance(p["due_date"], str) else p["due_date"],
            "amount_due": Decimal(str(p["amount_due"])),
        }
        for p in scaled_plan
    ]

    # Wallet IDs: get or create outside the transaction (idempotent)
    wallet_repo     = WalletRepository(db)
    debtor_wallet   = await wallet_repo.get_or_create("user", debtor_id)
    platform_wallet = await wallet_repo.get_or_create("platform", 1)

    result_payload: dict = {}

    async with db.atomic() as tx:
        order_tx      = CaptureOrderRepository(tx)
        req_tx        = CaptureRequestRepository(tx)
        position_tx   = CreditorPositionRepository(tx)
        instrument_tx = CreditInstrumentRepository(tx)
        wallet_tx     = WalletRepository(tx)

        # Idempotency guard inside transaction: if another concurrent call already
        # created the instrument, return it without duplication.
        existing = await instrument_tx.get_by_capture_order(capture_order_id)
        if existing:
            result_payload = {"ok": True, "instrument_id": existing["id"], "already_existed": True}
        else:
            # Mark order complete if still open
            if order["status"] == "open":
                await order_tx.update_status(
                    capture_order_id, "complete", completed_at=datetime.now(timezone.utc)
                )

            # Create instrument record
            instrument_id = await instrument_tx.create(
                capture_order_id=capture_order_id,
                debtor_id=debtor_id,
                total_amount=committed,
                interest_rate=Decimal(str(order["approved_rate"])),
                creditor_rate=Decimal(str(order["creditor_rate"])),
                term_days=req["term_days"],
                payment_plan_final=scaled_plan,
                origination_fee=origination_fee,
                origination_fee_pct=origination_fee_pct,
                servicing_fee_pct=servicing_fee_pct,
                net_disbursed_amount=net_disbursed,
                allows_assignment=True,
            )

            # Create installment schedule
            await instrument_tx.add_installments_bulk(instrument_id, installment_data)

            # Set participation fractions for confirmed creditor positions
            confirmed_positions = await position_tx.list_by_order(
                capture_order_id, status="confirmed"
            )
            for pos in confirmed_positions:
                pos_amount = Decimal(str(pos["committed_amount"]))
                fraction   = (pos_amount / committed).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP
                )
                await position_tx.set_participation_fraction(pos["id"], fraction)

            ref = str(instrument_id)

            # Platform escrow → debtor: only net_disbursed leaves the escrow.
            # origination_fee is implicitly retained (it was part of the gross escrow
            # deposited by creditors and was never released — no extra credit posted).
            await wallet_tx.add_transaction(
                wallet_id=platform_wallet["id"],
                amount=-net_disbursed,
                tx_type="p2p_capture_disbursement",
                reference_id=ref,
                reference_type="credit_instrument",
                description=f"P2P: disbursement to debtor for instrument #{instrument_id}",
            )
            await wallet_tx.add_transaction(
                wallet_id=debtor_wallet["id"],
                amount=net_disbursed,
                tx_type="p2p_capture_disbursement",
                reference_id=ref,
                reference_type="credit_instrument",
                description=f"P2P: loan received — instrument #{instrument_id}",
            )

            # Update capture request status
            await req_tx.update_status(order["capture_request_id"], "captured")

            result_payload = {
                "ok":              True,
                "instrument_id":   instrument_id,
                "net_disbursed":   net_disbursed,
                "origination_fee": origination_fee,
                "total_amount":    committed,
                "installments":    len(installment_data),
            }

    if result_payload.get("already_existed"):
        return result_payload

    instrument_id = result_payload.get("instrument_id")
    logger.info(
        "CreditInstrument emitted: id=%d order=%d debtor=%d amount=R$%.2f "
        "net=R$%.2f origination_fee=R$%.2f rate=%.4f%% installments=%d",
        instrument_id, capture_order_id, debtor_id,
        float(committed), float(net_disbursed), float(origination_fee),
        float(order["approved_rate"]) * 100, len(installment_data),
    )

    return result_payload


# ── 7. Process debtor payment ──────────────────────────────────────────────────

async def process_debtor_payment(
    db,
    *,
    credit_instrument_id: int,
    amount_paid: Decimal,
    payment_method: str = "pix",
    asaas_charge_id: Optional[str] = None,
) -> dict:
    """
    Step 7: Distributes a debtor payment to current creditors.

    Distribution is proportional to each creditor's participation_fraction
    at the time of payment (not original creditors — respects assignments).

    Servicing fee is deducted per creditor as a separate visible line.
    Platform fee is NEVER buried in the creditor's yield.

    All installment updates, wallet debits/credits, passthrough record, and
    instrument status change execute inside a single db.atomic() transaction.

    Returns: {ok, passthrough_id, total_distributed, servicing_fee_total,
              allocations, fully_paid}
    """
    from db.repositories.credit_instruments import CreditInstrumentRepository
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.installment_passthroughs import InstallmentPassthroughRepository
    from db.repositories.wallets import WalletRepository

    # ── Pre-flight reads (no mutations) ───────────────────────────────────────
    instrument = await CreditInstrumentRepository(db).get_by_id(credit_instrument_id)
    if not instrument:
        return {"ok": False, "error": "Credit instrument not found"}
    if instrument["status"] in ("paid_off", "renegotiated"):
        return {"ok": False, "error": f"Instrument already in status '{instrument['status']}'"}

    open_installments = await CreditInstrumentRepository(db).list_open_installments(
        credit_instrument_id
    )
    if not open_installments:
        return {"ok": False, "error": "No open installments to allocate payment against"}

    confirmed_positions = await CreditorPositionRepository(db).list_by_order(
        instrument["capture_order_id"], status="confirmed"
    )

    # Compute allocation plan (pure arithmetic — no DB)
    remaining_to_allocate = amount_paid
    allocation_plan: list[dict] = []
    for inst in open_installments:
        if remaining_to_allocate <= _ZERO:
            break
        to_allocate = min(Decimal(str(inst["remaining_amount"])), remaining_to_allocate)
        allocation_plan.append({"installment": inst, "to_allocate": to_allocate})
        remaining_to_allocate -= to_allocate

    primary_installment = open_installments[0]
    effective_amount    = amount_paid - remaining_to_allocate

    servicing_fee_pct = Decimal(str(instrument["servicing_fee_pct"]))
    distribution: list[dict] = []
    total_servicing = _ZERO

    for pos in confirmed_positions:
        fraction = Decimal(str(pos.get("participation_fraction") or 0))
        if fraction <= _ZERO:
            continue
        gross_amount  = (effective_amount * fraction).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        servicing_fee = (gross_amount * servicing_fee_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        distribution.append({
            "creditor_user_id": pos["creditor_user_id"],
            "position_id":      pos["id"],
            "gross_amount":     gross_amount,
            "servicing_fee":    servicing_fee,
            "net_amount":       gross_amount - servicing_fee,
        })
        total_servicing += servicing_fee

    total_net_distributed = effective_amount - total_servicing

    # Wallet IDs: get-or-create outside transaction (idempotent)
    wallet_repo     = WalletRepository(db)
    debtor_wallet   = await wallet_repo.get_or_create("user", instrument["debtor_id"])
    platform_wallet = await wallet_repo.get_or_create("platform", 1)
    creditor_wallets = {
        d["creditor_user_id"]: await wallet_repo.get_or_create("user", d["creditor_user_id"])
        for d in distribution
    }

    ref = f"inst_{credit_instrument_id}_{primary_installment['id']}"
    result_payload: dict = {}

    async with db.atomic() as tx:
        instrument_tx  = CreditInstrumentRepository(tx)
        passthrough_tx = InstallmentPassthroughRepository(tx)
        wallet_tx      = WalletRepository(tx)

        allocation_results: list[dict] = []
        for item in allocation_plan:
            new_status = await instrument_tx.apply_installment_allocation(
                item["installment"]["id"], item["to_allocate"]
            )
            allocation_results.append({
                "installment_id":     item["installment"]["id"],
                "sequence":           item["installment"]["sequence"],
                "allocated":          item["to_allocate"],
                "installment_status": new_status,
            })

        # Debit debtor wallet
        await wallet_tx.add_transaction(
            wallet_id=debtor_wallet["id"],
            amount=-effective_amount,
            tx_type="p2p_installment_passthrough",
            reference_id=ref,
            reference_type="credit_instrument",
            description=f"P2P: installment #{primary_installment['sequence']} for instrument #{credit_instrument_id}",
        )

        # Distribute to creditors
        for d in distribution:
            creditor_wallet = creditor_wallets[d["creditor_user_id"]]
            pos_ref = f"{ref}_pos{d['position_id']}"

            await wallet_tx.add_transaction(
                wallet_id=creditor_wallet["id"],
                amount=d["net_amount"],
                tx_type="p2p_installment_passthrough",
                reference_id=pos_ref,
                reference_type="creditor_position",
                description=f"P2P: passthrough (gross={d['gross_amount']}, servicing={d['servicing_fee']})",
            )
            if d["servicing_fee"] > _ZERO:
                await wallet_tx.add_transaction(
                    wallet_id=platform_wallet["id"],
                    amount=d["servicing_fee"],
                    tx_type="p2p_servicing_fee",
                    reference_id=pos_ref,
                    reference_type="creditor_position",
                    description=f"P2P: servicing fee from creditor #{d['creditor_user_id']}",
                )

        # Record passthrough
        passthrough_id = await passthrough_tx.create(
            credit_instrument_id=credit_instrument_id,
            installment_id=primary_installment["id"],
            installment_number=primary_installment["sequence"],
            total_amount_received=effective_amount,
            total_servicing_fee=total_servicing,
            total_net_distributed=total_net_distributed,
            distribution=distribution,
        )

        # Check and mark paid-off inside the transaction
        fully_paid = await instrument_tx.is_fully_paid(credit_instrument_id)
        if fully_paid:
            await instrument_tx.update_status(credit_instrument_id, "paid_off")

        result_payload = {
            "ok":                  True,
            "passthrough_id":      passthrough_id,
            "amount_received":     effective_amount,
            "total_distributed":   total_net_distributed,
            "total_servicing_fee": total_servicing,
            "creditors_paid":      len(distribution),
            "allocation_results":  allocation_results,
            "fully_paid":          fully_paid,
        }

    if result_payload.get("fully_paid"):
        logger.info("CreditInstrument %d fully paid off.", credit_instrument_id)

    logger.info(
        "P2P payment processed: instrument=%d passthrough=%d amount=R$%.2f "
        "distributed=R$%.2f servicing=R$%.2f creditors=%d fully_paid=%s",
        credit_instrument_id, result_payload["passthrough_id"], float(effective_amount),
        float(total_net_distributed), float(total_servicing),
        len(distribution), result_payload["fully_paid"],
    )
    return result_payload


# ── 8. Expire stale orders (background job) ────────────────────────────────────

async def expire_stale_orders(db) -> dict:
    """
    Background job: finds expired open CaptureOrders and triggers closure logic.

    Should be called periodically (e.g., every 15 minutes).
    """
    from db.repositories.capture_orders import CaptureOrderRepository

    order_repo = CaptureOrderRepository(db)
    expired_orders = await order_repo.list_expired_open()

    results = []
    for order in expired_orders:
        result = await check_and_close_order(db, order["id"])
        results.append({"order_id": order["id"], "result": result})

    logger.info("expire_stale_orders: processed %d expired orders", len(results))
    return {"processed": len(results), "results": results}


# ── Convenience: get capture order status ──────────────────────────────────────

async def get_capture_order_status(db, capture_order_id: int) -> dict:
    """Returns a consolidated status view of a CaptureOrder."""
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.credit_instruments import CreditInstrumentRepository

    order_repo      = CaptureOrderRepository(db)
    position_repo   = CreditorPositionRepository(db)
    instrument_repo = CreditInstrumentRepository(db)

    order = await order_repo.get_by_id(capture_order_id)
    if not order:
        return {}

    committed    = Decimal(str(order["committed_amount"]))
    target       = Decimal(str(order["target_amount"]))
    coverage_pct = float(committed / target * 100) if target > _ZERO else 0.0

    positions = await position_repo.list_by_order(capture_order_id)
    instrument = await instrument_repo.get_by_capture_order(capture_order_id)

    return {
        "order_id":          order["id"],
        "status":            order["status"],
        "target_amount":     target,
        "committed_amount":  committed,
        "minimum_threshold": Decimal(str(order["minimum_threshold"])),
        "coverage_pct":      coverage_pct,
        "capture_deadline":  order["capture_deadline"],
        "approved_rate":     Decimal(str(order["approved_rate"])),
        "creditor_rate":     Decimal(str(order["creditor_rate"])),
        "positions_total":   len(positions),
        "positions_confirmed": len([p for p in positions if p["status"] == "confirmed"]),
        "positions_reserved":  len([p for p in positions if p["status"] == "reserved"]),
        "instrument_id":     instrument["id"] if instrument else None,
    }


# ── Convenience: get debtor instrument summary ─────────────────────────────────

async def get_instrument_summary(db, credit_instrument_id: int) -> dict:
    """Returns a user-facing summary of a CreditInstrument."""
    from db.repositories.credit_instruments import CreditInstrumentRepository
    instrument_repo = CreditInstrumentRepository(db)
    return await instrument_repo.get_summary(credit_instrument_id)


async def get_user_instruments(db, user_id: int) -> list[dict]:
    """Returns all CreditInstruments where the user is the debtor."""
    from db.repositories.credit_instruments import CreditInstrumentRepository
    instrument_repo = CreditInstrumentRepository(db)
    instruments = await instrument_repo.list_by_debtor(user_id)
    results = []
    for inst in instruments:
        summary = await instrument_repo.get_summary(inst["id"])
        results.append(summary)
    return results

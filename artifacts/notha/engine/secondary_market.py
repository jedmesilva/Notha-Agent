"""
SecondaryMarket — pricing engine and settlement for P2P position assignments.

Regulatory compliance (SEP):
  1. Pricing MUST come from this engine — no free-text prices accepted.
  2. No structural exclusivity for any specific buyer (SCD or otherwise).
  3. SCD participation is never automatic or guaranteed.
  4. Debtor notification is mandatory before settlement (Art. 290 CC).
  5. No "guaranteed exit" messaging — liquidity depends on buyer availability.

Pricing formula:
  price = remaining_principal × discount_factor

  discount_factor = f(
    debtor_credit_score_current,  -- updated score, not score at issuance
    days_in_arrears,               -- 0 if current
    remaining_term_days,
    market_reference_rate,
  )
"""
import logging
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

logger = logging.getLogger("notha.secondary_market")

_ZERO = Decimal("0")
_ONE  = Decimal("1")


# ── 1. Price a creditor position ───────────────────────────────────────────────

async def price_position(
    db,
    position_id: int,
    market_reference_rate: Optional[Decimal] = None,
) -> dict:
    """
    Computes a fair price for a creditor position on the secondary market.

    Pricing inputs:
      - Current debtor credit score (not score at issuance)
      - Days in arrears (0 if no overdue installments)
      - Remaining term (days until last installment)
      - Market reference rate (from level_policies if not provided)

    Returns: {
      suggested_price, remaining_principal, discount_factor,
      pricing_breakdown, is_priceable
    }
    """
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.capture_requests import CaptureRequestRepository
    from db.repositories.credit_instruments import CreditInstrumentRepository
    from db.repositories.rates import RateRepository
    from engine.scoring_engine import get_or_compute_score

    position_repo   = CreditorPositionRepository(db)
    order_repo      = CaptureOrderRepository(db)
    req_repo        = CaptureRequestRepository(db)
    instrument_repo = CreditInstrumentRepository(db)
    rate_repo       = RateRepository(db)

    position = await position_repo.get_by_id(position_id)
    if not position or position["status"] != "confirmed":
        return {
            "is_priceable": False,
            "error": "Position not found or not in confirmed status",
        }

    order = await order_repo.get_by_id(position["capture_order_id"])
    if not order:
        return {"is_priceable": False, "error": "Capture order not found"}

    req = await req_repo.get_by_id(order["capture_request_id"])
    if not req:
        return {"is_priceable": False, "error": "Capture request not found"}

    instrument = await instrument_repo.get_by_capture_order(position["capture_order_id"])
    if not instrument or instrument["status"] not in ("active",):
        return {
            "is_priceable": False,
            "error": "Credit instrument not active — cannot price position",
        }

    debtor_id = instrument["debtor_id"]

    # ── Remaining principal for this position ──────────────────────────────────
    fraction = Decimal(str(position.get("participation_fraction") or 0))
    if fraction <= _ZERO:
        return {"is_priceable": False, "error": "Participation fraction not set"}

    open_installments = await instrument_repo.list_open_installments(instrument["id"])
    total_remaining = sum(Decimal(str(i["remaining_amount"])) for i in open_installments)
    remaining_principal = (total_remaining * fraction).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    if remaining_principal <= _ZERO:
        return {"is_priceable": False, "error": "No remaining principal to price"}

    # ── Current debtor credit score ────────────────────────────────────────────
    try:
        debtor_score = await get_or_compute_score(db, debtor_id)
    except Exception as exc:
        logger.warning("Could not get score for debtor %d: %s", debtor_id, exc)
        debtor_score = Decimal("500")  # neutral default

    # ── Days in arrears ────────────────────────────────────────────────────────
    overdue = [i for i in open_installments if i["status"] == "overdue"]
    days_in_arrears = 0
    if overdue:
        oldest_overdue = min(i["due_date"] for i in overdue)
        days_in_arrears = max(0, (date.today() - oldest_overdue).days)

    # ── Remaining term ─────────────────────────────────────────────────────────
    if open_installments:
        last_due = max(i["due_date"] for i in open_installments)
        remaining_term_days = max(0, (last_due - date.today()).days)
    else:
        remaining_term_days = 0

    # ── Market reference rate ──────────────────────────────────────────────────
    level_id = req["level_id"]
    if market_reference_rate is None:
        policy = await rate_repo.get_active_policy(level_id)
        market_reference_rate = (
            Decimal(str(policy["base_investment_rate"])) if policy else Decimal("0.02")
        )

    # ── Compute discount factor ────────────────────────────────────────────────
    discount_factor = _compute_discount_factor(
        debtor_score=float(debtor_score),
        days_in_arrears=days_in_arrears,
        remaining_term_days=remaining_term_days,
        market_reference_rate=float(market_reference_rate),
    )

    suggested_price = (remaining_principal * Decimal(str(discount_factor))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    pricing_breakdown = {
        "remaining_principal":    str(remaining_principal),
        "participation_fraction": str(fraction),
        "debtor_score_current":   float(debtor_score),
        "days_in_arrears":        days_in_arrears,
        "remaining_term_days":    remaining_term_days,
        "market_reference_rate":  float(market_reference_rate),
        "discount_factor":        discount_factor,
        "suggested_price":        str(suggested_price),
        "priced_at":              datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "Position %d priced: principal=R$%.2f discount=%.4f price=R$%.2f "
        "score=%.0f arrears=%dd term=%dd",
        position_id, float(remaining_principal), discount_factor,
        float(suggested_price), float(debtor_score),
        days_in_arrears, remaining_term_days,
    )

    return {
        "is_priceable":      True,
        "suggested_price":   suggested_price,
        "remaining_principal": remaining_principal,
        "discount_factor":   discount_factor,
        "pricing_breakdown": pricing_breakdown,
    }


def _compute_discount_factor(
    debtor_score: float,
    days_in_arrears: int,
    remaining_term_days: int,
    market_reference_rate: float,
) -> float:
    """
    Deterministic pricing formula for secondary market positions.

    Components:
      1. score_factor: higher score → closer to par (1.0)
      2. arrears_factor: penalty per day in arrears
      3. term_factor: longer remaining term → higher discount (time value)
      4. combined = score_factor × arrears_factor × term_factor

    Score range: 0–1000. Score 1000 → score_factor ~= 0.98 (2% haircut floor).
    Score 0 → score_factor ~= 0.50.
    """
    # Score factor: maps 0–1000 score to [0.50, 0.98] range
    score_normalized = max(0.0, min(1000.0, debtor_score)) / 1000.0
    score_factor = 0.50 + 0.48 * score_normalized  # [0.50, 0.98]

    # Arrears penalty: 1.5% per day in arrears (capped at 40% total)
    arrears_penalty = min(0.40, days_in_arrears * 0.015)
    arrears_factor = 1.0 - arrears_penalty

    # Term discount: time-value of money for remaining term
    # Longer term → buyer wants higher discount (more risk, more time)
    # Using market_reference_rate as daily rate proxy
    daily_rate = market_reference_rate / 365.0
    term_factor = 1.0 / (1.0 + daily_rate * remaining_term_days)

    discount_factor = score_factor * arrears_factor * term_factor
    # Floor: never go below 0.30 (30 cents on the dollar minimum)
    return round(max(0.30, min(1.0, discount_factor)), 6)


# ── 2. Propose assignment ──────────────────────────────────────────────────────

async def propose_assignment(
    db,
    *,
    position_id: int,
    buyer_user_id: int,
    requested_price: Optional[Decimal] = None,
) -> dict:
    """
    Seller (current creditor) initiates a secondary market transfer.

    The price is ALWAYS validated by the pricing engine — buyer cannot
    input an arbitrary price. If requested_price deviates >20% from
    engine price, the proposal is rejected.

    Returns: {ok, assignment_id, suggested_price, pricing_breakdown}
    """
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.assignment_transactions import AssignmentTransactionRepository

    position_repo    = CreditorPositionRepository(db)
    assignment_repo  = AssignmentTransactionRepository(db)

    position = await position_repo.get_by_id(position_id)
    if not position or position["status"] != "confirmed":
        return {"ok": False, "error": "Position not available for assignment"}

    # Verify seller is the current position holder (checked by caller context)
    already_pending = await assignment_repo.has_pending_for_position(position_id)
    if already_pending:
        return {"ok": False, "error": "There is already an active proposal for this position"}

    # Get engine-computed price
    pricing = await price_position(db, position_id)
    if not pricing.get("is_priceable"):
        return {"ok": False, "error": pricing.get("error", "Position cannot be priced")}

    suggested_price = pricing["suggested_price"]

    # If caller provided a requested_price, validate it against engine price
    final_price = suggested_price
    if requested_price is not None:
        tolerance = suggested_price * Decimal("0.20")  # 20% tolerance band
        lower_bound = suggested_price - tolerance
        upper_bound = suggested_price + tolerance
        if not (lower_bound <= requested_price <= upper_bound):
            return {
                "ok":    False,
                "error": (
                    f"Requested price R$ {requested_price:.2f} deviates more than 20% "
                    f"from engine price R$ {suggested_price:.2f}. "
                    f"Acceptable range: R$ {lower_bound:.2f} – R$ {upper_bound:.2f}."
                ),
                "suggested_price": suggested_price,
            }
        final_price = requested_price

    # Get credit_instrument_id
    from db.repositories.capture_orders import CaptureOrderRepository
    from db.repositories.credit_instruments import CreditInstrumentRepository
    order_repo      = CaptureOrderRepository(db)
    instrument_repo = CreditInstrumentRepository(db)

    order = await order_repo.get_by_id(position["capture_order_id"])
    if not order:
        return {"ok": False, "error": "Capture order not found"}

    instrument = await instrument_repo.get_by_capture_order(position["capture_order_id"])
    if not instrument:
        return {"ok": False, "error": "Credit instrument not found"}
    if not instrument["allows_assignment"]:
        return {"ok": False, "error": "This instrument does not allow position assignment"}

    assignment_id = await assignment_repo.create(
        credit_instrument_id=instrument["id"],
        seller_position_id=position_id,
        buyer_user_id=buyer_user_id,
        negotiated_price=final_price,
        pricing_breakdown=pricing["pricing_breakdown"],
    )

    logger.info(
        "AssignmentTransaction proposed: id=%d position=%d buyer=%d price=R$%.2f",
        assignment_id, position_id, buyer_user_id, float(final_price),
    )

    return {
        "ok":               True,
        "assignment_id":    assignment_id,
        "final_price":      final_price,
        "suggested_price":  suggested_price,
        "pricing_breakdown": pricing["pricing_breakdown"],
    }


# ── 3. Settle assignment ───────────────────────────────────────────────────────

async def settle_assignment(db, assignment_id: int) -> dict:
    """
    Finalises a secondary market assignment.

    Actions:
      1. Validates assignment is in 'accepted' status
      2. Transfers negotiated_price from buyer wallet to seller wallet
      3. Transfers position ownership (creditor_user_id) to buyer
      4. Records mandatory debtor notification date (Art. 290 CC)
      5. Marks assignment as 'settled'

    Returns: {ok, assignment_id, buyer_id, seller_id, price_settled}
    """
    from db.repositories.assignment_transactions import AssignmentTransactionRepository
    from db.repositories.creditor_positions import CreditorPositionRepository
    from db.repositories.wallets import WalletRepository

    assignment_repo = AssignmentTransactionRepository(db)
    position_repo   = CreditorPositionRepository(db)
    wallet_repo     = WalletRepository(db)

    assignment = await assignment_repo.get_by_id(assignment_id)
    if not assignment:
        return {"ok": False, "error": "Assignment not found"}
    if assignment["status"] != "accepted":
        return {"ok": False, "error": f"Assignment not in accepted status (status={assignment['status']})"}

    position = await position_repo.get_by_id(assignment["seller_position_id"])
    if not position or position["status"] != "confirmed":
        return {"ok": False, "error": "Seller position not available"}

    seller_id = position["creditor_user_id"]
    buyer_id  = assignment["buyer_user_id"]
    price     = Decimal(str(assignment["negotiated_price"]))

    buyer_wallet  = await wallet_repo.get_or_create("user", buyer_id)
    seller_wallet = await wallet_repo.get_or_create("user", seller_id)

    # Validate buyer has sufficient balance
    buyer_balance = await wallet_repo.true_balance(buyer_wallet["id"])
    if buyer_balance < price:
        return {
            "ok":    False,
            "error": f"Buyer insufficient balance: R$ {buyer_balance:.2f} < price R$ {price:.2f}",
        }

    ref = str(assignment_id)
    now = datetime.now(timezone.utc)

    async with db.atomic() as tx:
        from db.repositories.wallets import WalletRepository as _WR
        from db.repositories.creditor_positions import CreditorPositionRepository as _PR
        from db.repositories.assignment_transactions import AssignmentTransactionRepository as _AR

        wallet_tx    = _WR(tx)
        position_tx  = _PR(tx)
        assignment_tx = _AR(tx)

        # Buyer pays seller
        await wallet_tx.add_transaction(
            wallet_id=buyer_wallet["id"],
            amount=-price,
            tx_type="p2p_assignment_settlement",
            reference_id=ref,
            reference_type="assignment_transaction",
            description=f"P2P: purchase of creditor position #{position['id']}",
        )
        await wallet_tx.add_transaction(
            wallet_id=seller_wallet["id"],
            amount=price,
            tx_type="p2p_assignment_settlement",
            reference_id=ref,
            reference_type="assignment_transaction",
            description=f"P2P: proceeds from sale of creditor position #{position['id']}",
        )

        # Transfer position ownership
        await position_tx.transfer_to_buyer(position["id"], buyer_id)

        # Settle the assignment with debtor notification date (Art. 290 CC)
        await assignment_tx.settle(assignment_id, debtor_notification_date=now)

    logger.info(
        "AssignmentTransaction settled: id=%d position=%d seller=%d buyer=%d price=R$%.2f",
        assignment_id, position["id"], seller_id, buyer_id, float(price),
    )

    return {
        "ok":            True,
        "assignment_id": assignment_id,
        "buyer_id":      buyer_id,
        "seller_id":     seller_id,
        "price_settled": price,
        "debtor_notification_date": now.isoformat(),
    }

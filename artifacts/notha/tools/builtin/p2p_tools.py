"""
P2P Tools — conversational agent tools for the P2P debt issuance flow.

Exposes the P2P engine operations to the LLM agent as structured tool calls.
The LLM decides WHEN to call each tool; the engine executes deterministically.

Tools:
  Borrower side:
    - request_loan_p2p          : submit capture request (borrower intent)
    - launch_capture_order_tool : launch order after credit assessment
    - view_capture_status       : check order progress and funding coverage
    - pay_p2p_installment       : record a debtor payment and distribute

  Creditor/investor side:
    - commit_to_capture_order   : creditor reserves funds for an order
    - view_open_capture_orders  : list open P2P orders available to fund
    - view_creditor_positions   : view creditor's active positions
    - price_position_tool       : get engine-computed price for a position
    - propose_position_sale     : initiate secondary market transfer
"""
import logging
from decimal import Decimal, InvalidOperation
from tools.base import Tool

logger = logging.getLogger("notha.tools.p2p")


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
# BORROWER TOOLS
# ─────────────────────────────────────────────────────────────────────────────

class RequestLoanP2PTool(Tool):
    name = "request_loan_p2p"
    description = (
        "Submits a P2P loan capture request for the user (borrower). "
        "Creates a draft CaptureRequest — no capital is committed yet. "
        "Use when the user confirms they want to request a loan and provides "
        "the amount, term, and installment details. "
        "This does NOT approve or disburse anything — it only registers intent."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id":          {"type": "integer", "description": "Borrower's user ID"},
            "level_id":         {"type": "integer", "description": "Credit level (1–10)"},
            "requested_amount": {"type": "number",  "description": "Amount requested in BRL"},
            "term_days":        {"type": "integer", "description": "Total loan term in days"},
            "num_installments": {"type": "integer", "description": "Number of monthly installments (1–60)"},
            "first_due_days":   {
                "type": "integer",
                "description": "Days until first installment is due (default: 30)",
                "default": 30,
            },
        },
        "required": ["user_id", "level_id", "requested_amount", "term_days", "num_installments"],
    }

    async def execute(
        self,
        user_id: int,
        level_id: int,
        requested_amount: float,
        term_days: int,
        num_installments: int,
        first_due_days: int = 30,
    ) -> str:
        from db.connection import get_db
        from engine.p2p_engine import submit_capture_request

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        amount = _to_decimal(requested_amount)
        try:
            result = await submit_capture_request(
                db,
                user_id=user_id,
                level_id=level_id,
                requested_amount=amount,
                term_days=term_days,
                num_installments=num_installments,
                first_due_days=first_due_days,
            )

            if not result.get("ok"):
                return f"❌ Request rejected: {result.get('rejection_reason')}"

            req_id = result["capture_request_id"]
            score  = result.get("credit_score")
            plan   = result.get("payment_plan", [])

            installment_amount = _to_decimal(plan[0]["amount_due"]) if plan else amount / num_installments
            first_due = plan[0]["due_date"] if plan else "—"

            return (
                f"✅ *Capture request #{req_id} submitted!*\n\n"
                f"• Amount: {_fmt_brl(amount)}\n"
                f"• Term: {term_days} days ({num_installments} installments)\n"
                f"• Est. installment: {_fmt_brl(installment_amount)}\n"
                f"• First due: {first_due}\n"
                f"• Credit score: {score:.0f}/1000\n\n"
                f"Next step: credit assessment will launch a funding order "
                f"so investors can commit capital. I'll notify you when funding begins! 📊"
            )
        except Exception as exc:
            logger.error("request_loan_p2p error: %s", exc)
            return f"❌ Error submitting request: {exc}"


class LaunchCaptureOrderTool(Tool):
    name = "launch_capture_order"
    description = (
        "Launches a P2P CaptureOrder after credit assessment approves a CaptureRequest. "
        "Opens the order to creditors — they can now commit funds. "
        "Use after 'request_loan_p2p' and credit review. Admin/system use only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "capture_request_id":    {"type": "integer", "description": "ID of the CaptureRequest to launch"},
            "capture_window_hours":  {
                "type": "integer",
                "description": "Hours the order stays open for funding (default: 72)",
                "default": 72,
            },
        },
        "required": ["capture_request_id"],
    }

    async def execute(
        self,
        capture_request_id: int,
        capture_window_hours: int = 72,
    ) -> str:
        from db.connection import get_db
        from engine.p2p_engine import launch_capture_order

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        try:
            result = await launch_capture_order(
                db,
                capture_request_id=capture_request_id,
                capture_window_hours=capture_window_hours,
            )

            if not result.get("ok"):
                return f"❌ Launch failed: {result.get('rejection_reason')}"

            order_id  = result["capture_order_id"]
            rate      = result["approved_rate"]
            deadline  = result["capture_deadline"][:16].replace("T", " ")
            threshold = result["minimum_threshold"]
            orig_fee  = float(result["origination_fee_pct"]) * 100
            svc_fee   = float(result["servicing_fee_pct"]) * 100

            return (
                f"🚀 *CaptureOrder #{order_id} launched!*\n\n"
                f"• Approved rate: {_fmt_rate(rate)}\n"
                f"• Funding deadline: {deadline} UTC\n"
                f"• Minimum threshold: {_fmt_brl(threshold)}\n"
                f"• Origination fee: {orig_fee:.2f}% (charged to borrower)\n"
                f"• Servicing fee: {svc_fee:.2f}% per passthrough (charged to creditors)\n\n"
                f"Investors are being notified. Use *view_capture_status* to track progress."
            )
        except Exception as exc:
            logger.error("launch_capture_order error: %s", exc)
            return f"❌ Error launching order: {exc}"


class ViewCaptureStatusTool(Tool):
    name = "view_capture_status"
    description = (
        "Shows the current status of a P2P CaptureOrder: "
        "how much has been funded, coverage percentage, number of creditors committed, "
        "and whether the credit instrument has been emitted. "
        "Use when borrower or investor asks about order progress."
    )
    parameters = {
        "type": "object",
        "properties": {
            "capture_order_id": {"type": "integer", "description": "ID of the CaptureOrder"},
        },
        "required": ["capture_order_id"],
    }

    async def execute(self, capture_order_id: int) -> str:
        from db.connection import get_db
        from engine.p2p_engine import get_capture_order_status

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        try:
            s = await get_capture_order_status(db, capture_order_id)
            if not s:
                return f"❌ CaptureOrder #{capture_order_id} not found."

            status_icon = {
                "open":           "🔄",
                "complete":       "✅",
                "partial_expired": "⚠️",
                "cancelled":      "❌",
            }.get(s["status"], "⚪")

            deadline = str(s.get("capture_deadline", "—"))[:16].replace("T", " ")

            lines = [
                f"{status_icon} *CaptureOrder #{capture_order_id}*\n",
                f"  Status: {s['status']}",
                f"  Target: {_fmt_brl(s['target_amount'])}",
                f"  Committed (confirmed): {_fmt_brl(s['committed_amount'])} ({s['coverage_pct']:.1f}%)",
                f"  Minimum threshold: {_fmt_brl(s['minimum_threshold'])}",
                f"  Creditors: {s['positions_confirmed']} confirmed / {s['positions_reserved']} pending",
                f"  Approved rate: {_fmt_rate(s['approved_rate'])}",
                f"  Deadline: {deadline} UTC",
            ]

            if s.get("instrument_id"):
                lines.append(f"\n✅ Credit instrument #*{s['instrument_id']}* has been emitted and disbursed.")

            return "\n".join(lines)
        except Exception as exc:
            logger.error("view_capture_status error: %s", exc)
            return f"❌ Error: {exc}"


class PayP2PInstallmentTool(Tool):
    name = "pay_p2p_installment"
    description = (
        "Records a debtor payment against a P2P CreditInstrument and distributes "
        "the amount proportionally to current creditors (FIFO, servicing fee deducted). "
        "Use when the borrower confirms they have made a payment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "credit_instrument_id": {"type": "integer", "description": "ID of the CreditInstrument"},
            "amount_paid":          {"type": "number",  "description": "Amount paid in BRL"},
            "payment_method":       {
                "type": "string",
                "description": "Payment method (pix, boleto, transfer). Default: pix",
                "default": "pix",
            },
        },
        "required": ["credit_instrument_id", "amount_paid"],
    }

    async def execute(
        self,
        credit_instrument_id: int,
        amount_paid: float,
        payment_method: str = "pix",
    ) -> str:
        from db.connection import get_db
        from engine.p2p_engine import process_debtor_payment

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        amount = _to_decimal(amount_paid)
        if amount <= Decimal("0"):
            return "❌ Payment amount must be positive."

        try:
            result = await process_debtor_payment(
                db,
                credit_instrument_id=credit_instrument_id,
                amount_paid=amount,
                payment_method=payment_method,
            )

            if not result.get("ok"):
                return f"❌ {result.get('error')}"

            distributed   = result["total_distributed"]
            svc_fee       = result["total_servicing_fee"]
            creditors_paid = result["creditors_paid"]
            fully_paid    = result["fully_paid"]

            lines = [
                f"✅ *Payment of {_fmt_brl(amount)} recorded!*\n",
                f"  Distributed to {creditors_paid} creditor(s): {_fmt_brl(distributed)}",
                f"  Platform servicing fee: {_fmt_brl(svc_fee)}",
            ]

            allocs = result.get("allocation_results", [])
            for a in allocs:
                icon = "✅" if a["installment_status"] == "paid" else "🔄"
                lines.append(f"  {icon} Installment #{a['sequence']}: {_fmt_brl(a['allocated'])} applied")

            if fully_paid:
                lines.append("\n🎉 *Congratulations! Your debt is fully paid off!*")

            return "\n".join(lines)
        except Exception as exc:
            logger.error("pay_p2p_installment error: %s", exc)
            return f"❌ Error processing payment: {exc}"


class ViewUserInstrumentsTool(Tool):
    name = "view_user_instruments"
    description = (
        "Lists all P2P CreditInstruments where the user is the borrower/debtor. "
        "Shows status, amounts, next due date, and payment progress. "
        "Use when borrower asks about their active loans or payment schedule."
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "User ID (borrower)"},
        },
        "required": ["user_id"],
    }

    async def execute(self, user_id: int) -> str:
        from db.connection import get_db
        from engine.p2p_engine import get_user_instruments

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        try:
            summaries = await get_user_instruments(db, user_id)
            if not summaries:
                return "You have no active P2P instruments. Request a loan to get started! 😊"

            lines = [f"📋 *Your P2P credit instruments ({len(summaries)}):*\n"]
            for s in summaries:
                status_icon = {
                    "active": "🟡", "paid_off": "✅",
                    "defaulted": "🔴", "renegotiated": "🔵",
                }.get(s.get("status", ""), "⚪")

                next_due = s.get("next_due_date")
                next_due_str = str(next_due) if next_due else "—"

                lines.append(
                    f"{status_icon} *Instrument #{s['instrument_id']}*\n"
                    f"  Total: {_fmt_brl(s['total_amount'])} | Disbursed: {_fmt_brl(s['net_disbursed'])}\n"
                    f"  Origination fee: {_fmt_brl(s['origination_fee'])}\n"
                    f"  Rate: {_fmt_rate(s['interest_rate'])}\n"
                    f"  Paid: {_fmt_brl(s['total_paid'])} | Remaining: {_fmt_brl(s['total_remaining'])}\n"
                    f"  Installments: {s['open_installments']} open"
                    + (f" ⚠️ {s['overdue_installments']} overdue" if s.get("overdue_installments") else "")
                    + f"\n  Next due: {next_due_str}"
                )

            return "\n\n".join(lines)
        except Exception as exc:
            logger.error("view_user_instruments error: %s", exc)
            return f"❌ Error: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# CREDITOR/INVESTOR TOOLS
# ─────────────────────────────────────────────────────────────────────────────

class ViewOpenCaptureOrdersTool(Tool):
    name = "view_open_capture_orders"
    description = (
        "Lists open P2P CaptureOrders available for creditor funding. "
        "Use when an investor asks what P2P loans they can fund, "
        "or wants to see current funding opportunities."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum orders to show (default: 5)",
                "default": 5,
            },
        },
        "required": [],
    }

    async def execute(self, limit: int = 5) -> str:
        from db.connection import get_db
        from db.repositories.capture_orders import CaptureOrderRepository

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        try:
            repo = CaptureOrderRepository(db)
            orders = await repo.list_open(limit=limit)

            if not orders:
                return (
                    "No P2P capture orders are open right now. "
                    "When borrowers submit requests and pass credit review, "
                    "new orders appear here. 📭"
                )

            lines = [f"📈 *Open P2P funding opportunities ({len(orders)}):*\n"]
            for o in orders:
                target    = Decimal(str(o["target_amount"]))
                committed = Decimal(str(o["committed_amount"]))
                remaining = target - committed
                pct       = int(committed / target * 100) if target > 0 else 0
                deadline  = str(o.get("capture_deadline", "—"))[:16].replace("T", " ")

                lines.append(
                    f"🔹 *Order #{o['id']}*\n"
                    f"  Target: {_fmt_brl(target)}\n"
                    f"  Funded so far: {_fmt_brl(committed)} ({pct}%)\n"
                    f"  *Still available: {_fmt_brl(remaining)}*\n"
                    f"  Creditor rate: {_fmt_rate(o['creditor_rate'])}\n"
                    f"  Deadline: {deadline} UTC"
                )

            lines.append(
                "\n💡 To fund an order: \"I want to commit R$ X to order #ID\""
            )
            return "\n\n".join(lines)
        except Exception as exc:
            logger.error("view_open_capture_orders error: %s", exc)
            return f"❌ Error: {exc}"


class CommitToCaptureOrderTool(Tool):
    name = "commit_to_capture_order"
    description = (
        "Creditor commits funds to a P2P CaptureOrder. "
        "Reserves the amount from the creditor's wallet (held in escrow). "
        "The position becomes 'reserved' — it transitions to 'confirmed' "
        "once Pix receipt is verified. "
        "Use when investor confirms they want to fund a specific order."
    )
    parameters = {
        "type": "object",
        "properties": {
            "capture_order_id":  {"type": "integer", "description": "ID of the CaptureOrder to fund"},
            "creditor_user_id":  {"type": "integer", "description": "Creditor's user ID"},
            "amount":            {"type": "number",  "description": "Amount to commit in BRL"},
        },
        "required": ["capture_order_id", "creditor_user_id", "amount"],
    }

    async def execute(
        self,
        capture_order_id: int,
        creditor_user_id: int,
        amount: float,
    ) -> str:
        from db.connection import get_db
        from engine.p2p_engine import commit_creditor_position

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        commit_amount = _to_decimal(amount)
        if commit_amount <= Decimal("0"):
            return "❌ Amount must be positive."

        try:
            result = await commit_creditor_position(
                db,
                capture_order_id=capture_order_id,
                creditor_user_id=creditor_user_id,
                amount=commit_amount,
            )

            if not result.get("ok"):
                return f"❌ {result.get('error')}"

            pos_id = result["position_id"]
            return (
                f"✅ *Commitment registered!*\n\n"
                f"  Position #{pos_id} created\n"
                f"  Amount: {_fmt_brl(commit_amount)}\n"
                f"  Status: reserved (awaiting Pix confirmation)\n\n"
                f"Your funds are held in escrow. Once the platform confirms your "
                f"Pix receipt, the position transitions to *confirmed* and counts "
                f"toward the capture target. 🔒\n\n"
                f"If the order does not reach its minimum threshold before the "
                f"deadline, your funds are automatically returned. "
                f"Use *view_capture_status* to track order progress."
            )
        except Exception as exc:
            logger.error("commit_to_capture_order error: %s", exc)
            return f"❌ Error: {exc}"


class ViewCreditorPositionsTool(Tool):
    name = "view_creditor_positions"
    description = (
        "Shows a creditor's active positions across all P2P instruments. "
        "Displays committed amounts, participation fractions, and instrument status. "
        "Use when investor asks about their P2P investments or portfolio."
    )
    parameters = {
        "type": "object",
        "properties": {
            "creditor_user_id": {"type": "integer", "description": "Creditor's user ID"},
        },
        "required": ["creditor_user_id"],
    }

    async def execute(self, creditor_user_id: int) -> str:
        from db.connection import get_db
        from db.repositories.creditor_positions import CreditorPositionRepository

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        try:
            repo = CreditorPositionRepository(db)
            positions = await repo.list_by_creditor(creditor_user_id, limit=20)

            if not positions:
                return (
                    "You have no active P2P creditor positions. "
                    "Use *view_open_capture_orders* to find funding opportunities! 📭"
                )

            active   = [p for p in positions if p["status"] == "confirmed"]
            reserved = [p for p in positions if p["status"] == "reserved"]

            total_committed = sum(Decimal(str(p["committed_amount"])) for p in active)

            lines = [
                f"💼 *Your P2P creditor portfolio:*\n",
                f"  Active positions: {len(active)} | Pending: {len(reserved)}",
                f"  Total committed (confirmed): {_fmt_brl(total_committed)}\n",
            ]

            for p in positions[:10]:
                status_icon = {"confirmed": "✅", "reserved": "⏳", "reverted": "↩️"}.get(p["status"], "⚪")
                fraction_str = (
                    f"{float(p['participation_fraction']) * 100:.2f}%"
                    if p.get("participation_fraction") else "—"
                )
                rate_str = _fmt_rate(p.get("creditor_rate") or p.get("approved_rate", 0))

                lines.append(
                    f"{status_icon} *Position #{p['id']}* — Order #{p['capture_order_id']}\n"
                    f"  Committed: {_fmt_brl(p['committed_amount'])} | "
                    f"Fraction: {fraction_str}\n"
                    f"  Rate: {rate_str} | Status: {p['status']}"
                )

            if len(active) > 0:
                lines.append(
                    "\n💡 To sell a position on the secondary market, say: "
                    "\"Price position #ID\" or \"Sell position #ID\""
                )

            return "\n\n".join(lines)
        except Exception as exc:
            logger.error("view_creditor_positions error: %s", exc)
            return f"❌ Error: {exc}"


class PricePositionTool(Tool):
    name = "price_creditor_position"
    description = (
        "Gets the engine-computed fair market price for a creditor position "
        "on the P2P secondary market. Price is based on current debtor credit score, "
        "days in arrears, remaining term, and market reference rate. "
        "Use when creditor wants to sell their position or check its value."
    )
    parameters = {
        "type": "object",
        "properties": {
            "position_id": {"type": "integer", "description": "ID of the CreditorPosition to price"},
        },
        "required": ["position_id"],
    }

    async def execute(self, position_id: int) -> str:
        from db.connection import get_db
        from engine.secondary_market import price_position

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        try:
            result = await price_position(db, position_id)

            if not result.get("is_priceable"):
                return f"❌ Position cannot be priced: {result.get('error')}"

            price     = result["suggested_price"]
            principal = result["remaining_principal"]
            discount  = result["discount_factor"]
            bd        = result["pricing_breakdown"]

            score      = bd.get("debtor_score_current", "—")
            arrears    = bd.get("days_in_arrears", 0)
            term_left  = bd.get("remaining_term_days", "—")
            market_rate = bd.get("market_reference_rate", "—")

            haircut = (1 - discount) * 100

            return (
                f"📊 *Position #{position_id} — Secondary Market Price*\n\n"
                f"  Remaining principal: {_fmt_brl(principal)}\n"
                f"  *Suggested price: {_fmt_brl(price)}*\n"
                f"  Haircut: {haircut:.1f}%\n\n"
                f"  *Pricing factors:*\n"
                f"  • Debtor credit score: {score:.0f}/1000\n"
                f"  • Days in arrears: {arrears}\n"
                f"  • Remaining term: {term_left} days\n"
                f"  • Market reference rate: {float(market_rate)*100:.2f}% a.m.\n\n"
                f"⚠️ Note: secondary market liquidity depends on available buyers. "
                f"Exit is not guaranteed."
            )
        except Exception as exc:
            logger.error("price_creditor_position error: %s", exc)
            return f"❌ Error pricing position: {exc}"


class ProposePositionSaleTool(Tool):
    name = "propose_position_sale"
    description = (
        "Proposes a secondary market sale of a creditor position to a specific buyer. "
        "The price is engine-validated — deviations >20% from fair value are rejected. "
        "Use when a creditor confirms they want to sell and identifies a buyer. "
        "NEVER suggest guaranteed exit — liquidity depends on buyer interest."
    )
    parameters = {
        "type": "object",
        "properties": {
            "position_id":  {"type": "integer", "description": "ID of the CreditorPosition to sell"},
            "buyer_user_id": {"type": "integer", "description": "User ID of the prospective buyer"},
            "requested_price": {
                "type": "number",
                "description": "Requested sale price in BRL (optional — defaults to engine price)",
            },
        },
        "required": ["position_id", "buyer_user_id"],
    }

    async def execute(
        self,
        position_id: int,
        buyer_user_id: int,
        requested_price: float | None = None,
    ) -> str:
        from db.connection import get_db
        from engine.secondary_market import propose_assignment

        db = get_db()
        if not db:
            return "❌ Database unavailable."

        price = _to_decimal(requested_price) if requested_price is not None else None

        try:
            result = await propose_assignment(
                db,
                position_id=position_id,
                buyer_user_id=buyer_user_id,
                requested_price=price,
            )

            if not result.get("ok"):
                err = result.get("error", "Unknown error")
                suggested = result.get("suggested_price")
                if suggested:
                    return f"❌ {err}\n\n💡 Engine price: {_fmt_brl(suggested)}"
                return f"❌ {err}"

            assignment_id  = result["assignment_id"]
            final_price    = result["final_price"]
            suggested      = result["suggested_price"]

            return (
                f"📋 *Assignment proposal #{assignment_id} created!*\n\n"
                f"  Position: #{position_id}\n"
                f"  Buyer: user #{buyer_user_id}\n"
                f"  Final price: {_fmt_brl(final_price)}\n"
                f"  Engine suggested: {_fmt_brl(suggested)}\n\n"
                f"The buyer must accept the proposal before settlement. "
                f"Once accepted and settled, the position transfers and "
                f"the debtor is notified (Art. 290 CC). ⚖️"
            )
        except Exception as exc:
            logger.error("propose_position_sale error: %s", exc)
            return f"❌ Error proposing sale: {exc}"

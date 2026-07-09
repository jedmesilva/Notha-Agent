---
name: NOTHA P2P atomicity & race-safety rules
description: Critical atomicity decisions and race-condition fixes in the SEP P2P engine
---

## Rules

**emit_credit_instrument** — entire flow (instrument creation, installments, participation fractions, wallet disbursement, request status) must run inside `db.atomic()`. Idempotency guard (existing instrument check) re-runs inside the transaction. Origination fee is implicitly retained in escrow — do NOT post an extra `+origination_fee` credit to platform_wallet; doing so double-counts it.

**process_debtor_payment** — all installment allocations, wallet debits/credits (debtor, creditors, platform servicing fees), passthrough record, and paid-off status update run inside one `db.atomic()`. Compute the distribution plan (pure arithmetic) before entering the transaction.

**confirm_creditor_position** — wrap `position.confirm()` + `order.add_confirmed_commitment()` in `db.atomic()`. The confirm uses CAS (`WHERE status = 'reserved'`); the committed_amount increment uses `AND status = 'open'` guard so late confirms on already-closed orders are silent no-ops.

**add_confirmed_commitment** — single SQL statement that atomically increments committed_amount AND transitions status to 'complete' + sets completed_at in the same UPDATE. Never split into read-then-update.

**check_and_close_order expiry path** — when order expires below minimum_threshold, revert BOTH 'reserved' AND 'confirmed' positions. Confirmed creditors must be refunded when no instrument is emitted.

**_revert_position** — called inside per-position loops; each call does its own wallet transactions. For full-batch expiry, each position revert is independent but should be called within the order's expiry routine.

**Why:**
Concurrent Pix confirmations arriving milliseconds apart would double-count committed_amount without CAS+atomic wrapping. Partial failures in multi-table mutations leave orphaned instruments or stranded capital. Origination-fee double-credit was discovered in code review — the fee is retained by netting from gross escrow, not by a separate credit.

## levels table has no `status` column
`snapshot_liquidity` job: remove `WHERE lv.status = 'active'` — the `levels` table only has (id, name, description). All levels are implicitly active.

## investor_profiles.level_id — additive column migration
`_migrate_investor_profile_tables` must `ALTER TABLE investor_profiles ADD COLUMN IF NOT EXISTS level_id INT` before creating the index on (is_active, level_id). Same for investment_offers.level_id (allow NULL for old rows).

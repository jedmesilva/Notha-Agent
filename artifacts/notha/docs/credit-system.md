# NOTHA — Credit & Level System

> Reference manual for the credit, scoring, and level progression system.
> Keep this file updated whenever the rules or schema change.

---

## 1. Philosophy

**There is no consolidated credit score.**

Each decision (credit limit, interest rate, level upgrade) is made directly
from individual behavioral parameters. This ensures:

- Full auditability — every decision traces back to specific metrics.
- No hidden aggregation — a score of "620" tells you nothing; "82% on-time,
  0 active defaults, 14 loans completed" tells you everything.
- Independent axes — a user can have excellent payment history but low volume,
  and the system treats those dimensions separately.

---

## 2. Data layers

```
Raw events (debts, payments, investments)
        ↓  [computed by jobs]
User stats tables  (user_loan_stats, user_payment_stats, user_investment_stats)
        ↓  [read by algorithm]
user_credit_profile  (credit_limit, personal_risk_rate, default_rate)
        ↓  [used by engine]
Loan approval, rate pricing, level progression decisions
```

### 2.1 `user_loan_stats` — loan behavior

| Column | Type | Description |
|---|---|---|
| `requests_last_30d` | INT | Loan requests in the last 30 days |
| `requests_last_90d` | INT | Loan requests in the last 90 days |
| `requests_last_365d` | INT | Loan requests in the last 365 days |
| `grants_last_30d` | INT | Loans granted in the last 30 days |
| `grants_last_90d` | INT | Loans granted in the last 90 days |
| `grants_last_365d` | INT | Loans granted in the last 365 days |
| `grant_rate` | NUMERIC | `grants / requests` lifetime ratio |
| `total_requests_count` | INT | Lifetime loan request count |
| `total_grants_count` | INT | Lifetime loans granted count |
| `total_requested_amount` | NUMERIC | Lifetime total amount requested |
| `total_granted_amount` | NUMERIC | Lifetime total amount granted |
| `avg_requested_amount` | NUMERIC | Average ticket size requested |
| `avg_granted_amount` | NUMERIC | Average ticket size granted |
| `avg_utilization_rate` | NUMERIC | Average `granted / credit_limit` at time of loan |
| `max_utilization_ever` | NUMERIC | Peak utilization of available limit |
| `calculated_at` | TIMESTAMPTZ | When this row was last recalculated |

**What this tells the algorithm:**
- High `avg_utilization_rate` near 1.0 = user is always at the ceiling → financial stress signal.
- Low `grant_rate` = many requests denied → risk signal.
- Growth in `grants_last_30d` vs `grants_last_365d / 12` = acceleration in borrowing.

---

### 2.2 `user_payment_stats` — payment behavior

| Column | Type | Description |
|---|---|---|
| `on_time_rate` | NUMERIC | % of installments paid on or before due date |
| `early_rate` | NUMERIC | % of installments paid before due date |
| `late_rate` | NUMERIC | % of installments paid after due date |
| `avg_days_early` | NUMERIC | Mean days paid ahead when early |
| `avg_days_late` | NUMERIC | Mean days overdue when late |
| `payment_variance_days` | NUMERIC | Std deviation of payment timing (consistency) |
| `consecutive_on_time` | INT | Current streak of on-time installments |
| `max_consecutive_on_time` | INT | All-time longest on-time streak |
| `active_defaults_count` | INT | Installments currently in default |
| `total_defaults_count` | INT | Lifetime default count |
| `total_installments_paid` | INT | Lifetime installments paid |
| `calculated_at` | TIMESTAMPTZ | When this row was last recalculated |

**What this tells the algorithm:**
- `active_defaults_count > 0` = hard block on upgrades and new loans.
- `on_time_rate >= 0.95` + `consecutive_on_time >= 12` = strong reliability signal.
- `payment_variance_days` high = unpredictable payer even if mostly on time.

---

### 2.3 `user_investment_stats` — investment behavior

| Column | Type | Description |
|---|---|---|
| `has_ever_invested` | BOOLEAN | Has the user ever made an investment |
| `offers_received_count` | INT | Total investment offers received |
| `offers_accepted_count` | INT | Total investment offers accepted |
| `acceptance_rate` | NUMERIC | `accepted / received` |
| `total_invested_amount` | NUMERIC | Lifetime total capital deployed |
| `active_invested_amount` | NUMERIC | Current active investment position |
| `avg_investment_amount` | NUMERIC | Average investment ticket |
| `investments_active_count` | INT | Number of active investments |
| `investments_matured_count` | INT | Number of matured/liquidated investments |
| `calculated_at` | TIMESTAMPTZ | When this row was last recalculated |

**What this tells the algorithm:**
- Investment behavior at higher levels signals platform commitment and skin-in-the-game.
- `acceptance_rate` shows engagement: a user who receives 20 offers and invests in 15 is more engaged than one who accepts 1 of 20.

---

### 2.4 `user_credit_profile` — computed output

Calculated by the algorithm from the three stats tables above.
One row per user, updated by a periodic job.

| Column | Type | Description |
|---|---|---|
| `credit_limit` | NUMERIC | Current calculated credit limit |
| `personal_risk_rate` | NUMERIC | Risk premium added to base rate for this user |
| `default_rate` | NUMERIC | `total_defaults / total_installments_paid` |
| `calculated_at` | TIMESTAMPTZ | Last recalculation timestamp |
| `valid_until` | TIMESTAMPTZ | Expiry — recalculation required after this |

---

## 3. Level scoring rules

Each level defines its own rules. All thresholds must be met simultaneously
(AND logic — not weighted average).

### 3.1 `level_scoring_rules` — per-level configuration

#### Credit limit formula
| Column | Description |
|---|---|
| `credit_limit_base` | Floor — minimum limit at this level |
| `credit_limit_max` | Ceiling — maximum reachable limit at this level |
| `credit_limit_formula` | `linear` · `utilization_based` · `payment_weighted` |

**Formulas:**
- `linear` → `base + (on_time_rate) × (max - base)`
- `utilization_based` → `base + (1 - avg_utilization_rate) × (max - base)`
- `payment_weighted` → `base + (on_time_rate × (1 - default_rate)) × (max - base)`

#### Interest rate parameters
| Column | Description |
|---|---|
| `base_borrowing_rate` | Base rate charged to borrowers at this level |
| `risk_rate_multiplier` | Each unit of `personal_risk_rate` multiplies by this |
| `max_risk_premium` | Cap on total risk premium added to base rate |
| `base_investment_rate` | Base return rate paid to investors |
| `min_spread` | Minimum required spread: `borrowing_rate - investment_rate` |
| `spread_violation_strategy` | `reject_investment` or `raise_borrowing_rate` |

**Effective borrowing rate formula:**
```
effective_rate = base_borrowing_rate + min(default_rate × risk_rate_multiplier, max_risk_premium)
```

#### Pool exposure limits
| Column | Description |
|---|---|
| `max_aggregate_exposure` | Maximum total capital at risk in this level's pool |
| `default_individual_limit` | Fallback per-user limit when no individual limit exists |

---

### 3.2 Upgrade thresholds (to move to next level)

All of the following must be TRUE simultaneously:

| Column | Meaning |
|---|---|
| `min_on_time_rate_for_upgrade` | e.g. 0.90 → must have ≥ 90% on-time payments |
| `max_late_rate_for_upgrade` | e.g. 0.05 → must have ≤ 5% late payments |
| `max_defaults_ever_for_upgrade` | e.g. 0 → zero lifetime defaults allowed |
| `min_consecutive_on_time_for_upgrade` | e.g. 6 → current streak of ≥ 6 on-time payments |
| `min_grants_count_for_upgrade` | e.g. 3 → at least 3 loans fully repaid |
| `min_granted_amount_for_upgrade` | e.g. 500.00 → minimum lifetime credit volume |
| `max_avg_utilization_for_upgrade` | e.g. 0.85 → not consistently maxing out limit |
| `min_account_age_days_for_upgrade` | e.g. 90 → account must be ≥ 90 days old |
| `min_level_tenure_days_for_upgrade` | e.g. 30 → must have been at current level ≥ 30 days |
| `requires_investment_for_upgrade` | BOOLEAN — must have invested at least once |

**Hard blocks (override everything):**
- `active_defaults_count > 0` → upgrade impossible regardless of other metrics.

---

### 3.3 Downgrade thresholds (to demote to previous level)

| Column | Meaning |
|---|---|
| `min_on_time_rate_to_keep` | Falls below this → eligible for demotion |
| `max_active_defaults_to_keep` | Exceeds this → immediate demotion trigger |
| `max_consecutive_late_to_keep` | Too many consecutive late payments → demotion |

**Demotion is triggered by a job, not in real time.**
The job runs daily, evaluates all users at levels > 1, and flags candidates.
A human review step can be configured before demotion executes.

---

## 4. Level progression flow

```
[Daily job]
    ↓
Recalculate user_loan_stats, user_payment_stats, user_investment_stats
    ↓
Recalculate user_credit_profile (credit_limit, personal_risk_rate, default_rate)
    ↓
Evaluate upgrade candidates:
    user.current_level < 10
    AND active_defaults_count = 0
    AND all upgrade thresholds for current level met
    → INSERT into user_level_history (status='suggested')
    → Notify admin or auto-approve depending on config
    ↓
Evaluate downgrade candidates:
    any downgrade threshold violated
    → INSERT into user_level_history (status='suggested', direction='down')
```

---

## 5. Loan approval flow (using these parameters)

```
User requests loan (amount, term_days)
    ↓
Load user_credit_profile → credit_limit
Load user_payment_stats  → active_defaults_count
    ↓
Hard checks:
    active_defaults_count = 0          (hard block)
    amount <= credit_limit             (hard block)
    fund.status = 'active'             (hard block)
    user in fund (fund_users active)   (hard block)
    ↓
Rate calculation:
    effective_rate = base_borrowing_rate
                   + min(default_rate × risk_rate_multiplier, max_risk_premium)
                   + term_rate_adjustment(term_days)
    ↓
Exposure check:
    level.current_exposure_cache + amount <= max_aggregate_exposure
    ↓
Approve → create debt → create investment_opportunity
```

---

## 6. Files reference

| File | Purpose |
|---|---|
| `db/schema.sql` | Full database schema |
| `db/repositories/levels.py` | Level CRUD, policy, history, upgrade/downgrade |
| `db/repositories/user_stats.py` | Loan, payment, investment stats + credit profile |
| `db/repositories/funds.py` | Fund CRUD, policies, user allocation |
| `engine/rate_engine.py` | Rate calculation using level_scoring_rules |
| `engine/jobs.py` | Periodic jobs: stats recalculation, upgrade/downgrade evaluation |
| `engine/lending.py` | Loan approval flow |

---

## 7. Glossary

| Term | Definition |
|---|---|
| **Level** | Credit tier (1–10). Defines rules, limits, and rates for users at that tier. |
| **Fund** | Credit pool from which borrowers draw loans. Users are allocated to funds. |
| **Segment** | User category label. Used to filter investment opportunities for investors. |
| **Credit limit** | Maximum outstanding balance a user can hold at any time. |
| **Personal risk rate** | Individual risk premium added on top of the level's base borrowing rate. |
| **Default rate** | `total_defaults / total_installments_paid` — historical default frequency. |
| **Utilization rate** | `amount_granted / credit_limit` at the time of the loan. |
| **On-time rate** | `on_time_installments / total_installments_paid`. |
| **Spread** | Difference between borrowing rate and investment rate. Must stay above `min_spread`. |
| **Upgrade** | Transition to the next level (higher limit, better rates). |
| **Downgrade** | Demotion to a previous level due to deteriorating behavior. |

# Data dictionary

The dataset is fully synthetic and generated deterministically by `paysight data generate`
(seed 42, scale 1.0). It covers transactions from **2026-08-01 to 2026-10-31** (92 days).
Timestamps are naive IST. `AS_OF_DATE` is **2026-10-31** (end of day).

**All banks, PSPs, wallets and merchants are fictional.** City names are real places. Card BINs
all start with `9`, a range not used by the major card networks, so none of them maps to a real
issuer.

Sensitivity levels:

- `public`: safe for any role.
- `internal`: business data, available to every role.
- `pii`: personal or card data. Only the `risk_analyst` role may use it.

## Tables

### `dim_merchant` (400 rows)

| Column | Type | Meaning | Allowed values | Sensitivity |
|---|---|---|---|---|
| `merchant_id` | VARCHAR | Merchant key | `MER0001`… | internal |
| `merchant_name` | VARCHAR | Fictional merchant name | e.g. "NovaMart Electronics", "Orbit Electronics" | internal |
| `merchant_group` | VARCHAR, nullable | Parent group, if any | `NovaMart`, `StellarFuel`, `Mosaic Cafe`, NULL | internal |
| `category` | VARCHAR | Merchant category | grocery, electronics, fashion, travel, fuel, quick_commerce, pharmacy, dining | internal |
| `tier` | VARCHAR | Commercial tier | enterprise, mid_market, smb | internal |
| `region` | VARCHAR | Region (row-level security key) | north, south, east, west | internal |
| `city` | VARCHAR | City | real Indian cities | internal |
| `onboarded_on` | DATE | Onboarding date | 2019-01-01 … 2026-06-30 | internal |

NovaMart is three enterprise, online-only merchants (west, north and south regions). It
normally contributes about 6% of attempts.

### `dim_acquirer` (4 rows)

| Column | Type | Meaning | Allowed values | Sensitivity |
|---|---|---|---|---|
| `acquirer_id` | VARCHAR | Acquirer key | `ACQ_KESTREL`, `ACQ_MERIDIAN`, `ACQ_SAFFRON`, `ACQ_NORTHSTAR` | internal |
| `acquirer_name` | VARCHAR | Fictional acquiring bank | Kestrel Bank, Meridian Bank, Saffron Bank, Northstar Bank | internal |

Kestrel carries about 30% of card volume.

### `dim_issuer` (12 rows)

| Column | Type | Meaning | Allowed values | Sensitivity |
|---|---|---|---|---|
| `issuer_id` | VARCHAR | Issuer key | `BNK_*`, `PSP_*`, `WAL_*` | internal |
| `issuer_name` | VARCHAR | Fictional issuer, UPI PSP or wallet | Harbor, Lotus, Ridge, Indigo, Coral, Banyan Bank; Zephyr, PayNest, Tezra, Orchid; Pocketly, Walletra | internal |
| `issuer_type` | VARCHAR | Kind of issuer | bank, upi_psp, wallet | internal |

Banks issue cards and netbanking. UPI PSPs serve UPI, and wallets serve wallet payments. Harbor
Bank is about 25% of debit volume, and Zephyr about 30% of UPI.

### `dim_payment_method` (6 rows)

| Column | Type | Meaning | Allowed values | Sensitivity |
|---|---|---|---|---|
| `payment_method` | VARCHAR | Method key | UPI, CREDIT_CARD, DEBIT_CARD, CARD_EMI, NETBANKING, WALLET | public |
| `method_family` | VARCHAR | Family | upi, card, netbanking, wallet | public |

### `fact_payment_attempts` (attempt grain, about 1.15M rows)

Only the data-quality tool reads this raw table. Everything else uses `v_attempts_clean`.

| Column | Type | Meaning | Allowed values / notes | Sensitivity | Known quality issues |
|---|---|---|---|---|---|
| `attempt_id` | VARCHAR | Attempt key | `ATT00000001`… | internal | **Not unique**: 341 attempts on 2026-10-12 appear twice (S5) |
| `order_id` | VARCHAR | Order (checkout) key; retries share it | `ORD00000001`… | internal | |
| `attempt_number` | INTEGER | 1 for the first attempt, up to 3 | 1–3 | internal | |
| `created_at` | TIMESTAMP | When the attempt was made (IST) | 2026-08-01 … 2026-10-31 | internal | |
| `ingested_at` | TIMESTAMP | When the row landed in the warehouse | `created_at` + about 90s (max 1h) | internal | S5 duplicates have `ingested_at` +6h |
| `merchant_id` | VARCHAR | FK to `dim_merchant` | | internal | |
| `channel` | VARCHAR | Online checkout or in-store terminal | online (about 65%), pos | public | |
| `payment_method` | VARCHAR | FK to `dim_payment_method` | | public | |
| `acquirer_id` | VARCHAR | FK to `dim_acquirer` | | internal | |
| `issuer_id` | VARCHAR, nullable | FK to `dim_issuer` | | internal | **NULL for 8% of Meridian card attempts in August 2026** (S6) |
| `amount_inr` | DECIMAL(12,2) | Attempt amount in ₹ | log-normal by category; EMI ×2.5, min ₹3,000 | internal | |
| `status` | VARCHAR | Outcome | SUCCESS, FAILED | public | |
| `failure_code` | VARCHAR, nullable | Specific failure reason; NULL on success | e.g. ACQ_TIMEOUT, ISSUER_RISK_DECLINE, UPI_PSP_TIMEOUT, INSUFFICIENT_FUNDS, USER_CANCELLED | internal | |
| `failure_category` | VARCHAR, nullable | Failure family; NULL on success | customer_dropoff, issuer_decline, technical, risk_decline | public | |
| `latency_ms` | INTEGER | Authorization round-trip time | log-normal; timeouts are slow | internal | |
| `customer_ref_hash` | VARCHAR | Pseudonymous customer reference | 16 hex chars | **pii** | |
| `card_bin` | VARCHAR, nullable | First 6 digits of the card; NULL for non-card methods | `9xxxxx` (fictional) | **pii** | |

### `fact_settlements` (about 36K rows)

One batch per merchant per transaction date, settled T+1. Batches settled after `AS_OF_DATE`
do not exist yet, so transactions on 2026-10-31 are unsettled.

| Column | Type | Meaning | Sensitivity | Known quality issues |
|---|---|---|---|---|
| `settlement_batch_id` | VARCHAR | `STL-<merchant>-<covered_from>` | internal | |
| `merchant_id` | VARCHAR | FK to `dim_merchant` | internal | |
| `settlement_date` | DATE | Payout date | internal | NovaMart's combined batch settles on 2026-10-28 (S7) |
| `covered_from` / `covered_to` | DATE | Transaction dates covered | internal | Normally equal. NovaMart's batch covers 10-25..10-26 (S7) |
| `gross_amount_inr` | DECIMAL(14,2) | Successful, deduplicated GMV for the covered dates | internal | Orbit Electronics' 2026-10-14 batch is 1.8% short, unexplained (S7) |
| `refunds_inr` | DECIMAL(14,2) | Refunds netted off, 0.5–2% of gross | internal | |
| `fees_inr` | DECIMAL(14,2) | MDR-style fees: card 1.6%, netbanking 1.2%, wallet 1.5%, UPI 0% | internal | |
| `net_amount_inr` | DECIMAL(14,2) | gross − refunds − fees | internal | |
| `txn_count` | INTEGER | Successful attempts covered | internal | |

### `meta_data_version` (1 row)

Contains `generator_version`, `seed`, `scale`, `generated_at` (wall-clock build time) and
`content_hash`. `content_hash` is a sha256 over every data table. It is identical for identical
`(seed, scale)` and is part of the query-cache key.

## Governed views

| View | Grain | Notes |
|---|---|---|
| `v_attempts_clean` | attempt | Deduplicated: `ROW_NUMBER() OVER (PARTITION BY attempt_id ORDER BY ingested_at) = 1`. Joins all dimensions and adds `txn_date`, `hour_of_day`, `day_of_week`, `merchant_tier`, `acquirer_name`, `issuer_name`, `issuer_type` and `method_family`. |
| `v_orders` | order | One row per `order_id`. `created_at`, `payment_method` and `amount_inr` come from the first attempt. `final_payment_method`, `attempt_count`, and `order_status` = SUCCESS if any attempt succeeded. |
| `v_settlements` | batch | Settlements joined with merchant attributes, including `region` for row-level security. |

## Success-rate definitions

| Metric | Definition |
|---|---|
| Attempt success rate (**default**) | successful attempts / attempts |
| Order success rate | orders with any successful attempt / orders |
| Technical success rate | successful attempts / (attempts − customer drop-offs) |

Retries (S8) make attempt-level SR about 2.5pp lower than order-level SR.

## Planted scenarios: the answer key

Realized values at full scale, taken from [`data/ground_truth.yaml`](../data/ground_truth.yaml).
That file is the authoritative source and is regenerated with the data.

| # | Scenario | What was planted | Realized (seed 42, scale 1.0) |
|---|---|---|---|
| S1 | Acquirer outage | Kestrel Bank card attempts, 17 Sep 10:00 to 18 Sep 22:00, +35% `technical`/`ACQ_TIMEOUT` | Kestrel card SR 54.7% in the window (baseline 86.9%). 803 ACQ_TIMEOUTs, about 721 excess failures. Overall SR on 17 Sep is 3.0pp below the trailing 7-day median |
| S2 | Mix shift (hero) | NovaMart festive EMI/credit-card sale, 18–31 Oct. NovaMart goes from 6% to 30% of attempts | Attempt SR 90.0% → 88.5% (−1.47pp). By payment method: mix −1.00pp, rate −0.47pp, **mix share 68%** |
| S3 | Issuer risk declines | Harbor Bank debit from 21 Oct, +14% `risk_decline`/`ISSUER_RISK_DECLINE` | Harbor debit SR 85.8% → 74.5% (−11.3pp). This is 84% of S2's rate effect |
| S4 | PSP evening degradation | Zephyr UPI, 1–10 Sep, 19:00–22:59, +12% `technical`/`UPI_PSP_TIMEOUT` | Zephyr UPI evening SR 82.2% vs 93.5% on reference dates (−11.4pp). Daytime is unaffected (−0.5pp) |
| S5 | Duplicate ingestion | 3% of 12 Oct attempts re-ingested 6h later | 11,704 raw vs 11,363 clean (+3.0%). 341 duplicates, all on that day |
| S6 | Missing issuer IDs | Meridian card attempts, August | 8.0% NULL `issuer_id` (of 35,507). No NULLs anywhere else |
| S7 | Source disagreement | NovaMart's 25 Oct batch is missing and combined into a batch for 25–26 Oct settled on 28 Oct. Orbit Electronics' 14 Oct gross is 1.8% short | Combined batch present. Orbit is short by ₹11,199.34 (1.80%) with nothing in the data to explain it. 31 Oct is unsettled (timing) |
| S8 | Denominator gap | Drop-offs and technical failures are retried (p = 0.45, 60% switch to UPI) | Full period: attempt SR 89.6% vs order SR 92.1% (+2.5pp). Technical SR 93.3% |

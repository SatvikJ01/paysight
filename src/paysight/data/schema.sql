-- Paysight physical schema. Executed by the generator on a fresh database.
-- Timestamps are naive IST.

CREATE TABLE dim_merchant (
    merchant_id     VARCHAR PRIMARY KEY,
    merchant_name   VARCHAR NOT NULL,
    merchant_group  VARCHAR,
    category        VARCHAR NOT NULL,
    tier            VARCHAR NOT NULL,
    region          VARCHAR NOT NULL,
    city            VARCHAR NOT NULL,
    onboarded_on    DATE NOT NULL
);

CREATE TABLE dim_acquirer (
    acquirer_id     VARCHAR PRIMARY KEY,
    acquirer_name   VARCHAR NOT NULL
);

CREATE TABLE dim_issuer (
    issuer_id       VARCHAR PRIMARY KEY,
    issuer_name     VARCHAR NOT NULL,
    issuer_type     VARCHAR NOT NULL
);

CREATE TABLE dim_payment_method (
    payment_method  VARCHAR PRIMARY KEY,
    method_family   VARCHAR NOT NULL
);

-- Attempt grain. No primary key on attempt_id on purpose: S5 duplicates it.
CREATE TABLE fact_payment_attempts (
    attempt_id        VARCHAR NOT NULL,
    order_id          VARCHAR NOT NULL,
    attempt_number    INTEGER NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    ingested_at       TIMESTAMP NOT NULL,
    merchant_id       VARCHAR NOT NULL,
    channel           VARCHAR NOT NULL,
    payment_method    VARCHAR NOT NULL,
    acquirer_id       VARCHAR NOT NULL,
    issuer_id         VARCHAR,
    amount_inr        DECIMAL(12, 2) NOT NULL,
    status            VARCHAR NOT NULL,
    failure_code      VARCHAR,
    failure_category  VARCHAR,
    latency_ms        INTEGER NOT NULL,
    customer_ref_hash VARCHAR NOT NULL,
    card_bin          VARCHAR
);

CREATE TABLE fact_settlements (
    settlement_batch_id VARCHAR PRIMARY KEY,
    merchant_id         VARCHAR NOT NULL,
    settlement_date     DATE NOT NULL,
    covered_from        DATE NOT NULL,
    covered_to          DATE NOT NULL,
    gross_amount_inr    DECIMAL(14, 2) NOT NULL,
    refunds_inr         DECIMAL(14, 2) NOT NULL,
    fees_inr            DECIMAL(14, 2) NOT NULL,
    net_amount_inr      DECIMAL(14, 2) NOT NULL,
    txn_count           INTEGER NOT NULL
);

CREATE TABLE meta_data_version (
    generator_version VARCHAR NOT NULL,
    seed              INTEGER NOT NULL,
    scale             DOUBLE NOT NULL,
    generated_at      TIMESTAMP NOT NULL,
    content_hash      VARCHAR NOT NULL
);

-- Governed views. Only the data-quality tool may read fact_payment_attempts directly.

-- Clean attempts: S5 duplicates removed (first ingestion wins), all dimensions joined.
CREATE VIEW v_attempts_clean AS
WITH dedup AS (
    SELECT *
    FROM fact_payment_attempts
    QUALIFY ROW_NUMBER() OVER (PARTITION BY attempt_id ORDER BY ingested_at) = 1
)
SELECT
    d.attempt_id,
    d.order_id,
    d.attempt_number,
    d.created_at,
    d.ingested_at,
    CAST(d.created_at AS DATE) AS txn_date,
    CAST(EXTRACT(hour FROM d.created_at) AS INTEGER) AS hour_of_day,
    dayname(d.created_at) AS day_of_week,
    d.merchant_id,
    m.merchant_name,
    m.merchant_group,
    m.category,
    m.tier AS merchant_tier,
    m.region,
    m.city,
    d.channel,
    d.payment_method,
    pm.method_family,
    d.acquirer_id,
    a.acquirer_name,
    d.issuer_id,
    i.issuer_name,
    i.issuer_type,
    d.amount_inr,
    d.status,
    d.failure_code,
    d.failure_category,
    d.latency_ms,
    d.customer_ref_hash,
    d.card_bin
FROM dedup AS d
LEFT JOIN dim_merchant AS m ON d.merchant_id = m.merchant_id
LEFT JOIN dim_payment_method AS pm ON d.payment_method = pm.payment_method
LEFT JOIN dim_acquirer AS a ON d.acquirer_id = a.acquirer_id
LEFT JOIN dim_issuer AS i ON d.issuer_id = i.issuer_id;

-- Order grain: an order succeeds if any of its attempts succeeded (S8).
-- created_at, payment_method and amount_inr come from the first attempt.
CREATE VIEW v_orders AS
SELECT
    order_id,
    MIN(created_at) AS created_at,
    CAST(MIN(created_at) AS DATE) AS txn_date,
    CAST(EXTRACT(hour FROM MIN(created_at)) AS INTEGER) AS hour_of_day,
    dayname(MIN(created_at)) AS day_of_week,
    merchant_id,
    merchant_name,
    merchant_group,
    category,
    merchant_tier,
    region,
    city,
    channel,
    ARG_MIN(payment_method, attempt_number) AS payment_method,
    ARG_MIN(method_family, attempt_number) AS method_family,
    ARG_MAX(payment_method, attempt_number) AS final_payment_method,
    ARG_MIN(amount_inr, attempt_number) AS amount_inr,
    COUNT(*) AS attempt_count,
    CASE WHEN BOOL_OR(status = 'SUCCESS') THEN 'SUCCESS' ELSE 'FAILED' END AS order_status
FROM v_attempts_clean
GROUP BY
    order_id, merchant_id, merchant_name, merchant_group, category, merchant_tier,
    region, city, channel;

CREATE VIEW v_settlements AS
SELECT
    s.settlement_batch_id,
    s.merchant_id,
    m.merchant_name,
    m.merchant_group,
    m.category,
    m.tier AS merchant_tier,
    m.region,
    m.city,
    s.settlement_date,
    s.covered_from,
    s.covered_to,
    s.gross_amount_inr,
    s.refunds_inr,
    s.fees_inr,
    s.net_amount_inr,
    s.txn_count
FROM fact_settlements AS s
LEFT JOIN dim_merchant AS m ON s.merchant_id = m.merchant_id;

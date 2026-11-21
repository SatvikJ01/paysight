-- p95 authorization latency (ms) for credit cards, October 2026.
SELECT QUANTILE_CONT(latency_ms, 0.95) AS value FROM v_attempts_clean
WHERE payment_method = 'CREDIT_CARD'
  AND created_at >= TIMESTAMP '2026-10-01' AND created_at < TIMESTAMP '2026-11-01'

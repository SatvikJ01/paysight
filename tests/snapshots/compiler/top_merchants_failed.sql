SELECT
  merchant_name,
  SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_attempts,
  QUANTILE_CONT(latency_ms, 0.95) AS p95_latency_ms
FROM v_attempts_clean
WHERE
  (
    created_at >= CAST('2026-10-25' AS TIMESTAMP)
    AND created_at < CAST('2026-11-01' AS TIMESTAMP)
  )
  AND (
    NOT payment_method IN ('UPI', 'WALLET') OR payment_method IS NULL
  )
GROUP BY
  merchant_name
ORDER BY
  failed_attempts DESC
LIMIT 10

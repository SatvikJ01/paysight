SELECT
  CASE
    WHEN created_at >= CAST('2026-10-18' AS TIMESTAMP)
    AND created_at < CAST('2026-11-01' AS TIMESTAMP)
    THEN 'current'
    ELSE 'baseline'
  END AS comparison_window,
  payment_method,
  SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS attempt_success_rate_num,
  COUNT(*) AS attempt_success_rate_den,
  CAST(SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS DOUBLE) / NULLIF(COUNT(*), 0) AS attempt_success_rate
FROM v_attempts_clean
WHERE
  created_at >= CAST('2026-10-04' AS TIMESTAMP)
  AND created_at < CAST('2026-11-01' AS TIMESTAMP)
GROUP BY
  CASE
    WHEN created_at >= CAST('2026-10-18' AS TIMESTAMP)
    AND created_at < CAST('2026-11-01' AS TIMESTAMP)
    THEN 'current'
    ELSE 'baseline'
  END,
  payment_method
ORDER BY
  comparison_window,
  payment_method

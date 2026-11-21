SELECT
  DATE_TRUNC('DAY', created_at) AS period,
  SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS attempt_success_rate_num,
  COUNT(*) AS attempt_success_rate_den,
  CAST(SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS DOUBLE) / NULLIF(COUNT(*), 0) AS attempt_success_rate,
  SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_attempts
FROM v_attempts_clean
WHERE
  (
    created_at >= CAST('2026-09-10' AS TIMESTAMP)
    AND created_at < CAST('2026-09-21' AS TIMESTAMP)
  )
  AND acquirer_name = 'Kestrel Bank'
  AND method_family IN ('card')
GROUP BY
  DATE_TRUNC('DAY', created_at)
ORDER BY
  period

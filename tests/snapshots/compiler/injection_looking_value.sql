SELECT
  COUNT(*) AS attempts
FROM v_attempts_clean
WHERE
  (
    created_at >= CAST('2026-10-01' AS TIMESTAMP)
    AND created_at < CAST('2026-10-08' AS TIMESTAMP)
  )
  AND merchant_name = 'x'' OR 1=1; DROP TABLE t --'

-- NovaMart failed attempts, 25-31 Oct 2026.
SELECT SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS value FROM v_attempts_clean
WHERE merchant_group = 'NovaMart'
  AND created_at >= TIMESTAMP '2026-10-25' AND created_at < TIMESTAMP '2026-11-01'

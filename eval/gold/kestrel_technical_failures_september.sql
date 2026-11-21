-- Technical failures at the acquirer with the most of them in September 2026 (Kestrel Bank).
SELECT COUNT(*) AS value FROM v_attempts_clean
WHERE status <> 'SUCCESS' AND failure_category = 'technical' AND acquirer_name = 'Kestrel Bank'
  AND created_at >= TIMESTAMP '2026-09-01' AND created_at < TIMESTAMP '2026-10-01'

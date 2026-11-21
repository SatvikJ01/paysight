-- Card (credit, debit and EMI) attempt success rate, August 2026 (fraction).
SELECT AVG(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) AS value FROM v_attempts_clean
WHERE method_family = 'card'
  AND created_at >= TIMESTAMP '2026-08-01' AND created_at < TIMESTAMP '2026-09-01'

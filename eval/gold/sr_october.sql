-- Attempt-level success rate, October 2026 (fraction).
SELECT AVG(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) AS value FROM v_attempts_clean
WHERE created_at >= TIMESTAMP '2026-10-01' AND created_at < TIMESTAMP '2026-11-01'

-- Average latency (ms) of failed attempts over all available data (1 Aug - 31 Oct 2026).
SELECT AVG(latency_ms) AS value
FROM v_attempts_clean
WHERE status = 'FAILED'
  AND created_at >= TIMESTAMP '2026-08-01' AND created_at < TIMESTAMP '2026-11-01'

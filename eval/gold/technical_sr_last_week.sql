-- Technical success rate (drop-offs excluded), 25-31 Oct 2026 (fraction).
SELECT SUM(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0 END)
       / SUM(CASE WHEN COALESCE(failure_category, '') <> 'customer_dropoff' THEN 1 ELSE 0 END)
       AS value
FROM v_attempts_clean
WHERE created_at >= TIMESTAMP '2026-10-25' AND created_at < TIMESTAMP '2026-11-01'

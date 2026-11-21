-- Share of October 2026 orders that needed more than one payment attempt (fraction).
SELECT AVG(CASE WHEN attempt_count > 1 THEN 1.0 ELSE 0.0 END) AS value
FROM v_orders
WHERE created_at >= TIMESTAMP '2026-10-01' AND created_at < TIMESTAMP '2026-11-01'

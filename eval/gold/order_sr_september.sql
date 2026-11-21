-- Order-level success rate, September 2026 (fraction).
SELECT AVG(CASE WHEN order_status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) AS value FROM v_orders
WHERE created_at >= TIMESTAMP '2026-09-01' AND created_at < TIMESTAMP '2026-10-01'

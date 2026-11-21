-- Orders at Orbit Electronics, October 2026.
SELECT COUNT(*) AS value FROM v_orders
WHERE merchant_name = 'Orbit Electronics'
  AND created_at >= TIMESTAMP '2026-10-01' AND created_at < TIMESTAMP '2026-11-01'

SELECT
  channel,
  SUM(CASE WHEN order_status = 'SUCCESS' THEN 1 ELSE 0 END) AS order_success_rate_num,
  COUNT(*) AS order_success_rate_den,
  CAST(SUM(CASE WHEN order_status = 'SUCCESS' THEN 1 ELSE 0 END) AS DOUBLE) / NULLIF(COUNT(*), 0) AS order_success_rate,
  COUNT(*) AS orders
FROM v_orders
WHERE
  (
    created_at >= CAST('2026-09-01' AS TIMESTAMP)
    AND created_at < CAST('2026-09-11' AS TIMESTAMP)
  )
  AND hour_of_day BETWEEN 19 AND 22
GROUP BY
  channel
ORDER BY
  channel

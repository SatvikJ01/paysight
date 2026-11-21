SELECT
  CASE
    WHEN created_at >= CAST('2026-10-01' AS TIMESTAMP)
    AND created_at < CAST('2026-11-01' AS TIMESTAMP)
    THEN 'current'
    ELSE 'baseline'
  END AS comparison_window,
  SUM(CASE WHEN status = 'SUCCESS' THEN amount_inr ELSE 0 END) AS gmv_inr,
  SUM(CASE WHEN status = 'SUCCESS' THEN amount_inr ELSE 0 END) AS avg_ticket_inr_num,
  SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS avg_ticket_inr_den,
  CAST(SUM(CASE WHEN status = 'SUCCESS' THEN amount_inr ELSE 0 END) AS DOUBLE) / NULLIF(SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END), 0) AS avg_ticket_inr
FROM v_attempts_clean
WHERE
  (
    created_at >= CAST('2026-08-01' AS TIMESTAMP)
    AND created_at < CAST('2026-11-01' AS TIMESTAMP)
  )
  AND (
    (
      created_at >= CAST('2026-10-01' AS TIMESTAMP)
      AND created_at < CAST('2026-11-01' AS TIMESTAMP)
    )
    OR (
      created_at >= CAST('2026-08-01' AS TIMESTAMP)
      AND created_at < CAST('2026-09-01' AS TIMESTAMP)
    )
  )
GROUP BY
  CASE
    WHEN created_at >= CAST('2026-10-01' AS TIMESTAMP)
    AND created_at < CAST('2026-11-01' AS TIMESTAMP)
    THEN 'current'
    ELSE 'baseline'
  END
ORDER BY
  comparison_window

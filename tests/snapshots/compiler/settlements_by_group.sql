SELECT
  merchant_group,
  SUM(gross_amount_inr) AS settled_gross_inr,
  SUM(net_amount_inr) AS settled_net_inr
FROM v_settlements
WHERE
  (
    covered_from >= CAST('2026-10-25' AS DATE)
    AND covered_from <= CAST('2026-10-31' AS DATE)
  )
  AND merchant_group IS DISTINCT FROM 'StellarFuel'
GROUP BY
  merchant_group
ORDER BY
  merchant_group

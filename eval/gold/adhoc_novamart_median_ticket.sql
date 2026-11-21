-- Median successful attempt amount (INR) at NovaMart during its festive sale, 18-31 Oct 2026.
SELECT MEDIAN(amount_inr) AS value
FROM v_attempts_clean
WHERE merchant_group = 'NovaMart' AND status = 'SUCCESS'
  AND created_at >= TIMESTAMP '2026-10-18' AND created_at < TIMESTAMP '2026-11-01'

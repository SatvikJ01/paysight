-- Average successful ticket (INR), east region, September 2026.
SELECT SUM(CASE WHEN status = 'SUCCESS' THEN amount_inr ELSE 0 END)
       / SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS value
FROM v_attempts_clean
WHERE region = 'east' AND created_at >= TIMESTAMP '2026-09-01' AND created_at < TIMESTAMP '2026-10-01'

-- Deduplicated payment attempts on 12 Oct 2026.
SELECT COUNT(*) AS value FROM v_attempts_clean
WHERE created_at >= TIMESTAMP '2026-10-12' AND created_at < TIMESTAMP '2026-10-13'

-- =====================================================================
-- 02_classify.sql  -  turn match results into a list of breaks
-- One row per (trade, problem). A trade with two wrong fields gives two
-- rows, so an analyst sees every field that needs fixing.
-- =====================================================================
DROP TABLE IF EXISTS breaks_today;
CREATE TABLE breaks_today AS
WITH field_checks AS (
    -- For trades that matched on reference but not on fields,
    -- flag each field separately with CASE WHEN.
    SELECT m.*,
           CASE WHEN m.quantity    <> m.c_quantity    THEN 1 ELSE 0 END AS qty_diff,
           CASE WHEN ABS(m.price - m.c_price) > m.price_tol + 1e-9
                                                      THEN 1 ELSE 0 END AS price_diff,
           CASE WHEN m.settle_date <> m.c_settle_date THEN 1 ELSE 0 END AS settle_diff,
           CASE WHEN m.side        <> m.c_side        THEN 1 ELSE 0 END AS side_diff,
           CASE WHEN m.security_id <> m.c_security_id THEN 1 ELSE 0 END AS sec_diff
    FROM match_results m
    WHERE m.match_status = 'FIELD_BREAK'
),
field_breaks AS (
    SELECT external_ref, 'quantity_mismatch' AS break_type,
           'ours ' || quantity || ' vs custodian ' || c_quantity AS detail
    FROM field_checks WHERE qty_diff = 1
    UNION ALL
    SELECT external_ref, 'price_mismatch',
           'ours ' || price || ' vs custodian ' || c_price || ' (tol ' || ROUND(price_tol, 6) || ')'
    FROM field_checks WHERE price_diff = 1
    UNION ALL
    SELECT external_ref, 'settle_date_mismatch',
           'ours ' || settle_date || ' vs custodian ' || c_settle_date
    FROM field_checks WHERE settle_diff = 1
    UNION ALL
    SELECT external_ref, 'wrong_side',
           'ours ' || side || ' vs custodian ' || c_side
    FROM field_checks WHERE side_diff = 1
    UNION ALL
    SELECT external_ref, 'security_mismatch',
           'ours ' || security_id || ' vs custodian ' || c_security_id
    FROM field_checks WHERE sec_diff = 1
),
missing AS (
    SELECT external_ref,
           CASE match_status
                WHEN 'MISSING_AT_CUSTODIAN' THEN 'missing_at_custodian'
                WHEN 'MISSING_INTERNALLY'   THEN 'missing_internally'
           END AS break_type,
           CASE match_status
                WHEN 'MISSING_AT_CUSTODIAN' THEN 'booked by us, no custodian record'
                ELSE 'custodian record ' || cust_row_id || ', not in our books'
           END AS detail
    FROM match_results
    WHERE match_status IN ('MISSING_AT_CUSTODIAN', 'MISSING_INTERNALLY')
),
duplicates AS (
    -- rn > 1 from the ROW_NUMBER() in 01_match.sql = extra copies
    SELECT external_ref, 'duplicate_at_custodian' AS break_type,
           (MAX(rn)) || ' live copies at custodian' AS detail
    FROM custodian_asof
    GROUP BY external_ref
    HAVING MAX(rn) > 1
),
all_breaks AS (
    SELECT * FROM field_breaks
    UNION ALL SELECT * FROM missing
    UNION ALL SELECT * FROM duplicates
)
SELECT (SELECT as_of_date FROM params) AS as_of_date,
       a.external_ref, a.break_type, a.detail,
       m.trade_date, m.settle_date, m.asset_class, m.counterparty
FROM all_breaks a
JOIN match_results m ON m.external_ref = a.external_ref;

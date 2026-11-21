-- =====================================================================
-- 01_match.sql  -  build today's view of both sides and match them
-- Runs once per business day. The date being reconciled is in params.
-- =====================================================================

-- What is in OUR books as of today: anything booked on or before today.
-- (A trade booked late shows up only from the day it was booked.)
DROP TABLE IF EXISTS blotter_asof;
CREATE TABLE blotter_asof AS
SELECT b.*
FROM blotter b
WHERE b.booked_date <= (SELECT as_of_date FROM params);

-- What the CUSTODIAN says as of today: every NEW message received so far,
-- minus any message the custodian has since cancelled.
-- ROW_NUMBER() numbers the live messages per external_ref: rn = 1 is the
-- first one we received, rn > 1 means the custodian sent the same trade again.
DROP TABLE IF EXISTS custodian_asof;
CREATE TABLE custodian_asof AS
WITH cancelled AS (
    SELECT cancels_row_id
    FROM custodian
    WHERE msg_type = 'CANCEL'
      AND file_date <= (SELECT as_of_date FROM params)
),
live AS (
    SELECT c.*
    FROM custodian c
    WHERE c.msg_type = 'NEW'
      AND c.file_date <= (SELECT as_of_date FROM params)
      AND c.cust_row_id NOT IN (SELECT cancels_row_id FROM cancelled)
)
SELECT live.*,
       ROW_NUMBER() OVER (PARTITION BY external_ref
                          ORDER BY file_date, cust_row_id) AS rn
FROM live;

-- One row per trade with the outcome of matching.
--   EXACT     step 1: every field identical            -> straight-through (STP)
--   TOLERANCE step 2: same ref, only harmless price rounding inside tolerance
--   FIELD_BREAK       same ref, at least one field really differs
--   MISSING_AT_CUSTODIAN / MISSING_INTERNALLY  step 3: anti-joins
DROP TABLE IF EXISTS match_results;
CREATE TABLE match_results AS
WITH cust AS (                       -- first copy of each custodian trade only;
    SELECT * FROM custodian_asof     -- extra copies are handled as duplicates
    WHERE rn = 1
),
exact_match AS (                     -- STEP 1: exact composite key
    SELECT b.external_ref
    FROM blotter_asof b
    JOIN cust c
      ON  b.external_ref = c.external_ref
      AND b.security_id  = c.security_id
      AND b.side         = c.side
      AND b.quantity     = c.quantity
      AND b.price        = c.price
      AND b.settle_date  = c.settle_date
),
keyed AS (                           -- STEP 2: join on the reference only
    SELECT b.*,
           c.cust_row_id,
           c.security_id AS c_security_id,
           c.side        AS c_side,
           c.quantity    AS c_quantity,
           c.price       AS c_price,
           c.settle_date AS c_settle_date,
           -- Price tolerance: 0.01% of price or 1 cent, whichever is larger.
           -- FX rates are quoted to 4-5 decimals, so FX uses 0.01% only.
           CASE WHEN b.asset_class = 'fx' THEN 0.0001 * b.price
                ELSE MAX(0.01, 0.0001 * b.price) END AS price_tol
    FROM blotter_asof b
    JOIN cust c ON b.external_ref = c.external_ref
)
SELECT (SELECT as_of_date FROM params) AS as_of_date,
       k.external_ref, k.trade_id, k.trade_date, k.settle_date, k.asset_class,
       k.security_id, k.counterparty, k.side, k.quantity, k.price,
       k.cust_row_id, k.c_security_id, k.c_side, k.c_quantity, k.c_price,
       k.c_settle_date, k.price_tol,
       CASE
           WHEN k.external_ref IN (SELECT external_ref FROM exact_match) THEN 'EXACT'
           WHEN k.security_id = k.c_security_id
            AND k.side        = k.c_side
            AND k.quantity    = k.c_quantity
            AND k.settle_date = k.c_settle_date
            AND ABS(k.price - k.c_price) <= k.price_tol + 1e-9       THEN 'TOLERANCE'
           ELSE 'FIELD_BREAK'
       END AS match_status
FROM keyed k

UNION ALL
-- STEP 3a: we booked it, custodian has nothing (LEFT JOIN ... IS NULL)
SELECT (SELECT as_of_date FROM params),
       b.external_ref, b.trade_id, b.trade_date, b.settle_date, b.asset_class,
       b.security_id, b.counterparty, b.side, b.quantity, b.price,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       'MISSING_AT_CUSTODIAN'
FROM blotter_asof b
LEFT JOIN cust c ON b.external_ref = c.external_ref
WHERE c.external_ref IS NULL

UNION ALL
-- STEP 3b: custodian has it, we never booked it
SELECT (SELECT as_of_date FROM params),
       c.external_ref, NULL, c.trade_date, c.settle_date, c.asset_class,
       c.security_id, c.counterparty, NULL, NULL, NULL,
       c.cust_row_id, c.security_id, c.side, c.quantity, c.price, c.settle_date, NULL,
       'MISSING_INTERNALLY'
FROM cust c
LEFT JOIN blotter_asof b ON b.external_ref = c.external_ref
WHERE b.external_ref IS NULL;

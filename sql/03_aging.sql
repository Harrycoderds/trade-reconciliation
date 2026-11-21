-- =====================================================================
-- 03_aging.sql  -  keep a history of breaks, age the open ones, KPIs
-- Age is counted in BUSINESS days: a break first seen today is 1 day old.
-- Buckets: 1 day | 2-3 days | 4 days (watch list) | 5+ days (escalate)
-- =====================================================================

-- 1) Append today's breaks to the running history (safe to re-run a day).
CREATE TABLE IF NOT EXISTS break_history AS SELECT * FROM breaks_today WHERE 0;
DELETE FROM break_history WHERE as_of_date = (SELECT as_of_date FROM params);
INSERT INTO break_history SELECT * FROM breaks_today;

-- 2) Open breaks with their age.
-- "Gaps and islands": business-day number minus ROW_NUMBER() is constant
-- while a break is open on consecutive days, so it identifies each
-- continuous run. The age is measured from the start of the current run.
DROP TABLE IF EXISTS open_breaks;
CREATE TABLE open_breaks AS
WITH today AS (
    SELECT p.as_of_date, c.bus_day_no
    FROM params p JOIN calendar c ON c.bus_date = p.as_of_date
),
hist AS (
    SELECT h.external_ref, h.break_type, h.as_of_date, c.bus_day_no,
           c.bus_day_no - ROW_NUMBER() OVER (PARTITION BY h.external_ref, h.break_type
                                             ORDER BY c.bus_day_no) AS island
    FROM break_history h
    JOIN calendar c ON c.bus_date = h.as_of_date
),
runs AS (
    SELECT external_ref, break_type, island,
           MIN(as_of_date) AS first_seen,
           MIN(bus_day_no) AS first_no,
           MAX(bus_day_no) AS last_no
    FROM hist
    GROUP BY external_ref, break_type, island
),
aged AS (
    SELECT b.*,
           r.first_seen,
           t.bus_day_no - r.first_no + 1 AS age_bus_days,
           CAST(julianday(t.as_of_date) - julianday(r.first_seen) AS INTEGER) AS age_calendar_days,
           -- past settlement date and still broken = likely settlement fail
           MAX(0, CAST(julianday(t.as_of_date) - julianday(b.settle_date) AS INTEGER)) AS days_past_settle
    FROM breaks_today b
    CROSS JOIN today t
    JOIN runs r
      ON  r.external_ref = b.external_ref
      AND r.break_type   = b.break_type
      AND r.last_no      = t.bus_day_no
)
SELECT aged.*,
       CASE WHEN age_bus_days <= 1 THEN '1 day'
            WHEN age_bus_days <= 3 THEN '2-3 days'
            WHEN age_bus_days = 4  THEN '4 days'
            ELSE '5+ days' END AS age_bucket,
       CASE WHEN age_bus_days >= 5 THEN 'ESCALATE to supervisor/counterparty manager. ' ELSE '' END ||
       CASE break_type
            WHEN 'quantity_mismatch'      THEN 'Check trade ticket and allocation vs broker confirm; amend our booking or ask custodian to correct.'
            WHEN 'price_mismatch'         THEN 'Compare execution price with broker confirm; agree correct price with counterparty.'
            WHEN 'settle_date_mismatch'   THEN 'Confirm settlement cycle (T+1/T+2) with counterparty; amend instruction.'
            WHEN 'missing_at_custodian'   THEN 'Chase custodian/broker for the instruction; confirm trade was affirmed.'
            WHEN 'missing_internally'     THEN 'Check with front office for an unbooked trade; book it or reject the custodian record.'
            WHEN 'wrong_side'             THEN 'URGENT: confirm direction with trader and counterparty; position is wrong both ways.'
            WHEN 'duplicate_at_custodian' THEN 'Ask custodian to cancel the duplicate instruction to avoid double settlement.'
            ELSE 'Investigate manually.' END AS suggested_action
FROM aged;

-- 3) Daily KPIs (one row per business day).
-- STP = trades of the day that matched automatically (exact or within
-- tolerance) with no break of any kind, divided by trades of the day.
CREATE TABLE IF NOT EXISTS daily_kpis (
    as_of_date TEXT PRIMARY KEY, new_trades INTEGER, exact_matched INTEGER,
    tolerance_matched INTEGER, stp_trades INTEGER, stp_rate REAL, exact_rate REAL,
    new_breaks INTEGER, resolved_today INTEGER, open_breaks INTEGER,
    open_1d INTEGER, open_2_3d INTEGER, open_4d INTEGER, open_5plus INTEGER
);
DELETE FROM daily_kpis WHERE as_of_date = (SELECT as_of_date FROM params);
INSERT INTO daily_kpis
WITH todays_trades AS (
    SELECT DISTINCT external_ref, match_status
    FROM match_results
    WHERE trade_date = (SELECT as_of_date FROM params)
),
broken AS (SELECT DISTINCT external_ref FROM breaks_today),
prev_day AS (
    SELECT MAX(bus_date) AS d FROM calendar
    WHERE bus_date < (SELECT as_of_date FROM params)
),
resolved AS (   -- open yesterday, gone today
    SELECT h.external_ref, h.break_type
    FROM break_history h
    WHERE h.as_of_date = (SELECT d FROM prev_day)
    EXCEPT
    SELECT external_ref, break_type FROM breaks_today
),
stp AS (
    SELECT COUNT(*) AS n_trades,
           SUM(match_status = 'EXACT') AS n_exact,
           SUM(match_status = 'TOLERANCE') AS n_tol,
           SUM(match_status IN ('EXACT', 'TOLERANCE')
               AND external_ref NOT IN (SELECT external_ref FROM broken)) AS n_stp,
           SUM(match_status = 'EXACT'
               AND external_ref NOT IN (SELECT external_ref FROM broken)) AS n_exact_clean
    FROM todays_trades
)
SELECT (SELECT as_of_date FROM params),
       n_trades, n_exact, n_tol, n_stp,
       ROUND(1.0 * n_stp / n_trades, 4),
       ROUND(1.0 * n_exact_clean / n_trades, 4),
       (SELECT COUNT(*) FROM open_breaks WHERE age_bus_days = 1),
       (SELECT COUNT(*) FROM resolved),
       (SELECT COUNT(*) FROM open_breaks),
       (SELECT COUNT(*) FROM open_breaks WHERE age_bucket = '1 day'),
       (SELECT COUNT(*) FROM open_breaks WHERE age_bucket = '2-3 days'),
       (SELECT COUNT(*) FROM open_breaks WHERE age_bucket = '4 days'),
       (SELECT COUNT(*) FROM open_breaks WHERE age_bucket = '5+ days')
FROM stp;

-- 4) Breakdowns as of today. "Breaks raised" counts each (trade, break type)
-- once over the whole period; "open" is what is still unresolved today.
DROP TABLE IF EXISTS kpi_by_counterparty;
CREATE TABLE kpi_by_counterparty AS
WITH trades AS (
    SELECT counterparty, COUNT(DISTINCT external_ref) AS trades
    FROM match_results GROUP BY counterparty
),
raised AS (
    SELECT counterparty, COUNT(*) AS breaks_raised
    FROM (SELECT DISTINCT external_ref, break_type, counterparty FROM break_history)
    GROUP BY counterparty
),
open_now AS (
    SELECT counterparty, COUNT(*) AS open_breaks, SUM(age_bus_days >= 5) AS open_5plus
    FROM open_breaks GROUP BY counterparty
)
SELECT t.counterparty, t.trades,
       COALESCE(r.breaks_raised, 0) AS breaks_raised,
       ROUND(1.0 * COALESCE(r.breaks_raised, 0) / t.trades, 4) AS breaks_per_trade,
       COALESCE(o.open_breaks, 0) AS open_breaks,
       COALESCE(o.open_5plus, 0) AS open_5plus
FROM trades t
LEFT JOIN raised r   ON r.counterparty = t.counterparty
LEFT JOIN open_now o ON o.counterparty = t.counterparty
ORDER BY breaks_raised DESC, t.counterparty;

DROP TABLE IF EXISTS kpi_by_type;
CREATE TABLE kpi_by_type AS
WITH raised AS (
    SELECT break_type, COUNT(*) AS breaks_raised
    FROM (SELECT DISTINCT external_ref, break_type FROM break_history)
    GROUP BY break_type
)
SELECT r.break_type, r.breaks_raised,
       COUNT(o.external_ref) AS open_breaks,
       COALESCE(SUM(o.age_bus_days >= 5), 0) AS open_5plus,
       ROUND(AVG(o.age_bus_days), 2) AS avg_open_age
FROM raised r
LEFT JOIN open_breaks o ON o.break_type = r.break_type
GROUP BY r.break_type
ORDER BY r.breaks_raised DESC;

DROP TABLE IF EXISTS kpi_by_asset_class;
CREATE TABLE kpi_by_asset_class AS
WITH trades AS (
    SELECT asset_class, COUNT(DISTINCT external_ref) AS trades
    FROM match_results GROUP BY asset_class
),
raised AS (
    SELECT asset_class, COUNT(*) AS breaks_raised
    FROM (SELECT DISTINCT external_ref, break_type, asset_class FROM break_history)
    GROUP BY asset_class
)
SELECT t.asset_class, t.trades, COALESCE(r.breaks_raised, 0) AS breaks_raised,
       ROUND(1.0 * COALESCE(r.breaks_raised, 0) / t.trades, 4) AS breaks_per_trade,
       (SELECT COUNT(*) FROM open_breaks o WHERE o.asset_class = t.asset_class) AS open_breaks
FROM trades t
LEFT JOIN raised r ON r.asset_class = t.asset_class
ORDER BY t.asset_class;

-- 5) Daily log of every record's status (feeds the Tableau export).
CREATE TABLE IF NOT EXISTS recon_log AS
SELECT m.as_of_date, m.external_ref, m.trade_date, m.asset_class, m.counterparty,
       m.match_status, o.break_type, o.age_bus_days, o.age_bucket
FROM match_results m
LEFT JOIN open_breaks o ON o.external_ref = m.external_ref
WHERE 0;
DELETE FROM recon_log WHERE as_of_date = (SELECT as_of_date FROM params);
INSERT INTO recon_log
SELECT m.as_of_date, m.external_ref, m.trade_date, m.asset_class, m.counterparty,
       m.match_status, o.break_type, o.age_bus_days, o.age_bucket
FROM match_results m
LEFT JOIN open_breaks o ON o.external_ref = m.external_ref;

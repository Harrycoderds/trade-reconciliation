# Trade Reconciliation & Exception Engine

A small Python + SQLite tool that simulates a middle-office daily trade reconciliation: it compares our internal trade blotter against a custodian/broker file, finds the differences ("breaks"), sorts them by type and age, and produces an Excel exceptions report plus a CSV for a Tableau dashboard.

All data is synthetic. Every number below comes from running `python run_all.py` with the fixed seed (42).

## The problem in plain English

When a fund or bank trades, two records exist: what **we** booked, and what the **custodian/broker** says happened. Before the trade settles (one business day later for most North American securities since May 2024), operations must confirm both records agree on security, side, quantity, price and settlement date. Any difference is a *break*. Unresolved breaks become settlement fails, which cost money (funding, claims, penalties), create wrong positions and cash, and attract regulators' attention. A middle-office analyst's morning is largely: run the recon, work the exception list, oldest and most dangerous breaks first.

## What the engine does

1. **Generates data** (`src/generate_data.py`): about 4,000 equity, bond and FX trades over 10 business days, a custodian file built from the same trades, and errors injected into the custodian side. Each injected error is written to `ground_truth.csv` (the answer key).
2. **Reconciles day by day in SQL** (`src/reconcile.py` + `sql/`): for each business day it only "sees" trades booked so far and custodian messages received so far, then matches, classifies and ages the breaks.
3. **Scores itself** against the labelled synthetic breaks (precision and recall per break type).
4. **Reports** (`src/report.py`): `reports/recon_report.xlsx` and `reports/tableau_export.csv`.

## Data design and assumptions

| Item | Assumption |
|---|---|
| Period | 10 business days, 2026-09-14 to 2026-09-25 (no US/Canadian holidays in that window; weekends skipped, holidays otherwise ignored) |
| Volume | 300-500 trades per day, mix ~55% equity / 25% bond / 20% FX |
| Securities | Fictional: equity tickers ending `.SYN`, bond ISIN-format codes with a fake `SY` prefix and a valid check digit, real FX currency pairs with made-up rates |
| Settlement | Equities and bonds T+1. FX spot T+2, except USD/CAD T+1 |
| Price tolerance | Equity/bond: max(1 cent, 0.01% of price). FX: 0.01% of the rate. Quantity, side, security and settlement date must match exactly |
| Custodian file | A stream of `NEW` and `CANCEL` messages with a `file_date`, like real cancel/correct messaging |
| Persistence | Each open break is fixed with 35% probability per day (custodian cancel + correct, late instruction, or our late booking), so breaks age realistically; some are still open at the end |
| Counterparties | 12 fictional names. **Their error rates are a design choice in the generator** (some are deliberately more error-prone), so any counterparty ranking in the report is true by construction. It is not a finding |

**Injected breaks** (about 4% of trades on average):

1. Quantity mismatch
2. Price mismatch (beyond tolerance)
3. Settlement date mismatch
4. Missing at custodian (we booked it, they have nothing)
5. Missing internally (they have it, we never booked it)
6. Wrong side (buy vs sell)
7. Duplicate at custodian

Extra test cases:
- **Within-tolerance rounding** (about 3% of clean trades): e.g. a half-cent difference. These must *not* be flagged.
- **Two-error trades** (about 8% of field breaks): e.g. quantity *and* side wrong.
- **Mis-keyed reference (`ref_mismatch`)**, a deliberate hard case: the custodian dropped a digit from our reference. It is not one of the 7 engine categories, and it is there to measure a known weakness.

## Matching logic (all in SQL)

| File | What it does | SQL features |
|---|---|---|
| `sql/01_match.sql` | Builds "as of today" views of both sides (custodian messages minus cancellations). **Step 1:** exact match on reference + security + side + quantity + price + settle date → `EXACT`. **Step 2:** join on reference only and compare fields with tolerance → `TOLERANCE` if only a harmless price difference, else `FIELD_BREAK`. **Step 3:** anti-joins (`LEFT JOIN ... WHERE ... IS NULL`) both ways → missing at custodian / missing internally | CTEs, `ROW_NUMBER()` to number custodian copies of the same trade, `CASE WHEN` |
| `sql/02_classify.sql` | Flags each differing field separately, so a two-error trade gives two break rows. `rn > 1` from `ROW_NUMBER()` = duplicate | CTEs, `CASE WHEN`, `UNION ALL` |
| `sql/03_aging.sql` | Appends to `break_history`, ages open breaks in business days ("gaps and islands": business-day number minus `ROW_NUMBER()` identifies each continuous run), days past settlement with `julianday()`, suggested action per type, daily KPIs and breakdowns | Window functions, `julianday()`, `EXCEPT`, aggregates |

**Definitions**
- **STP rate**: trades of the day matched automatically (exact, or only a within-tolerance price difference) with no break of any kind, divided by all trades of the day. The exact-only rate is reported alongside.
- **Break**: one (trade, problem) pair.
- **Age**: business days since the break was first seen; first seen today = 1 day.
- **Age buckets**: `1 day`, `2-3 days`, `4 days` (its own watch-list bucket, amber), `5+ days` (escalate, red).

## Results (seed 42)

**Volume:** 4,037 records reconciled over 10 business days (4,024 booked trades, 4,166 custodian messages). 153 breaks raised in total.

**STP:** average 96.3% per day (range 94.8%-97.5%). Exact-match-only average 93.3%. The gap is the 119 within-tolerance rounding differences, which were all auto-matched.

**Open breaks on the last day (2026-09-25):** 49 in total: 15 at 1 day, 20 at 2-3 days, 10 at 4 days, 4 at 5+ days.

**Accuracy, measured against labelled synthetic breaks:**

| Break type | Labelled | Detected | Precision | Recall |
|---|---|---|---|---|
| quantity_mismatch | 29 | 29 | 100% | 100% |
| price_mismatch | 16 | 16 | 100% | 100% |
| settle_date_mismatch | 16 | 16 | 100% | 100% |
| wrong_side | 21 | 21 | 100% | 100% |
| duplicate_at_custodian | 15 | 15 | 100% | 100% |
| missing_at_custodian | 25 | 32 | 78.1% | 100% |
| missing_internally | 17 | 24 | 70.8% | 100% |
| ref_mismatch (hard case) | 7 | 0 | n/a | 0% |
| **All** | 146 | 153 | 90.8% | 95.2% |

Within-tolerance differences: 119 labelled, 0 wrongly flagged. The 4 two-error trades were each reported as two breaks, as designed.

**How to read this honestly:**
- All 14 false positives come from the 7 mis-keyed references. The engine matches on the exact reference, so one mis-keyed trade looks like two unrelated breaks (missing at custodian + missing internally). A real desk would pair these up by matching on secondary fields (security, quantity, date, counterparty). I left that out on purpose to keep the matching simple.
- The 100% scores on the other types are **not evidence the engine is good on real data**. The generator and the matcher were built together by the same person, so the errors are exactly the kinds the matcher looks for. Real custodian files would contain formats, codes and errors this engine has never seen.

## Limitations

- Synthetic data only; the generator and matcher were built together, so scores overstate real-world performance.
- Counterparty error rates are set in the generator, so `By_Counterparty` shows a pattern by construction, not an insight. With ~300-400 trades per counterparty it is also noisy: the most error-prone counterparty in the generator is not the top one in this run.
- Simplified conventions: no holidays, no partial settlements, no corporate actions, no fees/commissions, no cash or position reconciliation, no FX forwards/swaps, no CSDR penalties.
- Matching is exact-key only (no fuzzy or secondary-field matching), so mis-keyed references are not recognised.
- Resolution is random (35% per day), not driven by analyst work.
- Security mismatch is supported by the SQL but never injected.

## How to run (Mac)

```bash
cd ~/Desktop/trade-reconciliation
python3 -m venv .venv            # first time only
source .venv/bin/activate
pip install -r requirements.txt  # first time only
python run_all.py                # whole pipeline, a few seconds
python -m unittest discover tests -v
```

Outputs: `data/raw/*.csv`, `data/recon.db`, `reports/recon_report.xlsx`, `reports/tableau_export.csv`.

### Excel report sheets

| Sheet | Content |
|---|---|
| Summary | Latest-day KPIs, open breaks by age bucket (5+ red, 4 amber), breaks by type |
| Open_Breaks | One row per open break: id, type, counterparty, asset class, age, bucket, days past settle, detail, suggested action. 5+ days in red |
| Daily_KPIs | STP rate, exact rate, new / resolved / open breaks and buckets per day |
| By_Counterparty | Trades, breaks raised, breaks per trade, open breaks per counterparty (by construction, see note), plus by asset class |
| Accuracy | Precision/recall per type, measured against labelled synthetic breaks |
| Assumptions | Everything above in one place |

## Tableau Public dashboard (step by step)

`reports/tableau_export.csv` has one row per (business day, record, open break). Columns: `as_of_date, trade_date, external_ref, asset_class, counterparty, match_status, status (Auto-matched/Break), break_type ('none' if matched), age_bus_days, age_bucket, is_new_trade`.

1. Open Tableau Public → *Connect → Text file* → choose `tableau_export.csv`. Check `as_of_date` and `trade_date` are Date type.
2. **STP trend:** new sheet. Filter `is_new_trade` = 1. Create calculated field `STP Rate` = `COUNTD(IF [status] = "Auto-matched" THEN [external_ref] END) / COUNTD([external_ref])`. Put `as_of_date` (exact date, discrete) on Columns, `STP Rate` on Rows, mark type Line. Format as percentage.
3. **Breaks by type:** new sheet. Filter `status` = Break and `age_bus_days` = 1 (counts each break once, on the day it appeared). `break_type` on Rows, `CNT(external_ref)` on Columns, sort descending.
4. **Aging buckets:** new sheet. Filter `status` = Break and `as_of_date` = 2026-09-25 (latest day). `age_bucket` on Columns (sort manually: 1 day, 2-3 days, 4 days, 5+ days), `CNT(external_ref)` on Rows. Drag `age_bucket` to Color and set 5+ days red.
5. **Top counterparties:** new sheet. Filter `status` = Break and `age_bus_days` = 1. `counterparty` on Rows, `CNT(external_ref)` on Columns, sort descending, Top 10 filter. Add a caption: "Counterparty error rates are set in the synthetic data generator, so this ranking is by construction."
6. *Dashboard → New Dashboard*, drag the four sheets in, then *File → Save to Tableau Public*.

## Optional VBA macro

`excel/highlight_aged_breaks.bas` sorts `Open_Breaks` by age (oldest first) and colours 5+ day breaks red. The Python pipeline does not need it.

1. Open `reports/recon_report.xlsx` in Excel → *File → Save As* → *Excel Macro-Enabled Workbook (.xlsm)*.
2. *Tools → Macro → Visual Basic Editor* (or Option+F11).
3. *File → Import File…* → choose `excel/highlight_aged_breaks.bas`.
4. Close the editor, *Tools → Macro → Macros…* → `HighlightAgedBreaks` → *Run*.

## Project structure

```
trade-reconciliation/
├── data/raw/            internal_blotter.csv, custodian_file.csv, ground_truth.csv
├── data/recon.db        SQLite (rebuilt each run)
├── sql/                 01_match.sql, 02_classify.sql, 03_aging.sql
├── src/                 generate_data.py, reconcile.py, report.py
├── excel/               highlight_aged_breaks.bas
├── tests/test_recon.py  18 unit tests on tiny hand-built examples
├── reports/             recon_report.xlsx, tableau_export.csv
├── docs/screenshots/    placeholder for report and dashboard screenshots
└── run_all.py
```

## Screenshots

`docs/screenshots/` is a placeholder for the Excel Summary/Open_Breaks sheets and the Tableau dashboard.

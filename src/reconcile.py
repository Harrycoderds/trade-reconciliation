"""
reconcile.py - loads the CSVs into SQLite and runs the SQL day by day.

Each business day we pretend it is the end of that day: only trades booked so far and
custodian messages received so far are visible. The three .sql files then match, classify
and age the breaks. Ground truth is loaded ONLY after all days have run, for scoring.
"""
import os
import sqlite3

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_DIR = os.path.join(ROOT, "sql")
DB_PATH = os.path.join(ROOT, "data", "recon.db")
SQL_FILES = ["01_match.sql", "02_classify.sql", "03_aging.sql"]

# The 7 categories the engine can output (plus security_mismatch, never injected).
ENGINE_TYPES = ["quantity_mismatch", "price_mismatch", "settle_date_mismatch",
                "missing_at_custodian", "missing_internally", "wrong_side",
                "duplicate_at_custodian"]


def connect(path=DB_PATH):
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return sqlite3.connect(path)


def load_raw(conn, blotter, custodian, days, extra_days=5):
    """Load both sides plus a business-day calendar (window + a few days for settle dates)."""
    blotter.to_sql("blotter", conn, if_exists="replace", index=False)
    custodian.to_sql("custodian", conn, if_exists="replace", index=False)
    cal = pd.bdate_range(days[0], periods=len(days) + extra_days)
    pd.DataFrame({"bus_date": cal.strftime("%Y-%m-%d"),
                  "bus_day_no": range(1, len(cal) + 1)}).to_sql(
        "calendar", conn, if_exists="replace", index=False)
    for t in ["break_history", "daily_kpis", "recon_log"]:   # start clean
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


def run_day(conn, as_of):
    """Run the three SQL files for one business day."""
    conn.execute("DROP TABLE IF EXISTS params")
    conn.execute("CREATE TABLE params (as_of_date TEXT)")
    conn.execute("INSERT INTO params VALUES (?)", (as_of,))
    for f in SQL_FILES:
        with open(os.path.join(SQL_DIR, f)) as fh:
            conn.executescript(fh.read())
    conn.commit()


def run_all_days(conn, days):
    for d in days:
        run_day(conn, d)
    return pd.read_sql("SELECT * FROM daily_kpis ORDER BY as_of_date", conn)


def accuracy(conn, truth):
    """
    Score the engine against the labelled synthetic breaks.
    Unit = (external_ref, break_type), counted once over the whole period.
    TP = engine raised it and it is in the ground truth; FP = raised but not labelled;
    FN = labelled but never raised. 'within_tolerance' rows are not breaks: any flag on
    them is a false positive, reported separately.
    """
    truth.to_sql("ground_truth", conn, if_exists="replace", index=False)
    det = pd.read_sql("SELECT DISTINCT external_ref, break_type FROM break_history", conn)
    gt = truth[truth["break_type"] != "within_tolerance"][["external_ref", "break_type"]].drop_duplicates()

    d_set = set(map(tuple, det.values))
    g_set = set(map(tuple, gt.values))
    # refs touched by the deliberate hard case (our ref and the mis-keyed custodian ref)
    rm = truth[truth["break_type"] == "ref_mismatch"]
    ref_mismatch_refs = set(rm["external_ref"]) | set(rm["custodian_ref"].dropna())
    rows = []
    for bt in sorted(set(gt["break_type"]) | set(det["break_type"])):
        d = {x for x in d_set if x[1] == bt}
        g = {x for x in g_set if x[1] == bt}
        tp, fp, fn = len(d & g), len(d - g), len(g - d)
        rows.append({"break_type": bt, "labelled": len(g), "detected": len(d), "true_pos": tp,
                     "false_pos": fp, "fp_from_ref_mismatch": sum(x[0] in ref_mismatch_refs for x in d - g),
                     "false_neg": fn,
                     "precision": tp / (tp + fp) if tp + fp else None,
                     "recall": tp / (tp + fn) if tp + fn else None,
                     "engine_category": bt in ENGINE_TYPES})
    acc = pd.DataFrame(rows)
    tp, fp, fn = acc["true_pos"].sum(), acc["false_pos"].sum(), acc["false_neg"].sum()
    acc = pd.concat([acc, pd.DataFrame([{
        "break_type": "ALL", "labelled": len(g_set), "detected": len(d_set), "true_pos": tp,
        "false_pos": fp, "fp_from_ref_mismatch": acc["fp_from_ref_mismatch"].sum(),
        "false_neg": fn, "precision": tp / (tp + fp), "recall": tp / (tp + fn),
        "engine_category": None}])], ignore_index=True)

    wt = set(truth.loc[truth["break_type"] == "within_tolerance", "external_ref"])
    flagged_wt = len(wt & set(det["external_ref"]))
    return acc, {"within_tolerance_labelled": len(wt), "within_tolerance_flagged": flagged_wt}

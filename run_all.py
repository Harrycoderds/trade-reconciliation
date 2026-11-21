"""
run_all.py - runs the whole pipeline end to end.

    python run_all.py

1. generate synthetic blotter + custodian file + ground truth   (src/generate_data.py)
2. load into SQLite and reconcile each business day             (src/reconcile.py + sql/*.sql)
3. score the engine against the labelled synthetic breaks
4. write reports/recon_report.xlsx and reports/tableau_export.csv (src/report.py)
"""
import glob
import os
import sys

import pandas as pd

from src import generate_data as gen
from src import reconcile as rec
from src import report


def sanity_checks(conn, t, acc, wt_info):
    """Hard checks must hold; weak accuracy is reported as a warning, never tuned away."""
    errors, notes = [], []

    # 1) Matching logic must not see the answer key.
    for f in glob.glob(os.path.join(rec.SQL_DIR, "*.sql")):
        if "ground_truth" in open(f).read():
            errors.append(f"{os.path.basename(f)} references ground_truth")

    # 2) Every record on the last day is accounted for exactly once.
    q = lambda s: conn.execute(s).fetchone()[0]
    booked, matched_rows = q("SELECT COUNT(*) FROM blotter_asof"), q(
        "SELECT COUNT(*) FROM match_results WHERE match_status <> 'MISSING_INTERNALLY'")
    if booked != matched_rows:
        errors.append(f"blotter rows {booked} != matched rows {matched_rows}")
    cust_first, cust_rows = q("SELECT COUNT(*) FROM custodian_asof WHERE rn = 1"), q(
        "SELECT COUNT(*) FROM match_results WHERE match_status <> 'MISSING_AT_CUSTODIAN'")
    if cust_first != cust_rows:
        errors.append(f"custodian rows {cust_first} != matched rows {cust_rows}")

    # 3) KPIs in range.
    k = t["kpis"]
    if not k["stp_rate"].between(0, 1).all():
        errors.append("STP rate outside [0, 1]")
    if (t["open"]["age_bus_days"] < 1).any():
        errors.append("open break with age < 1")
    if k["open_breaks"].iloc[-1] != len(t["open"]):
        errors.append("open break count mismatch between KPIs and Open_Breaks")

    # 4) Honest warnings.
    if wt_info["within_tolerance_flagged"]:
        notes.append(f"{wt_info['within_tolerance_flagged']} within-tolerance differences were flagged as breaks.")
    for _, r in acc[acc["break_type"] != "ALL"].iterrows():
        if r["recall"] is not None and r["recall"] < 0.9:
            why = (" The engine has no category for a mis-keyed reference: it shows up as one "
                   "missing_at_custodian + one missing_internally break." if r["break_type"] == "ref_mismatch" else "")
            notes.append(f"Recall for {r['break_type']} is {r['recall']:.0%}.{why}")
        if r["precision"] is not None and r["precision"] < 0.9:
            notes.append(f"Precision for {r['break_type']} is {r['precision']:.0%} "
                         f"({int(r['false_pos'])} false positives, {int(r['fp_from_ref_mismatch'])} of them "
                         f"caused by mis-keyed references).")
    return errors, notes


def main():
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    days = gen.business_days()

    print("1) Generating synthetic data ...")
    blotter, custodian, truth = gen.generate()
    print(f"   blotter {len(blotter)} trades | custodian {len(custodian)} messages | "
          f"ground truth {len(truth)} labels -> {gen.RAW_DIR}")

    print("2) Reconciling day by day in SQLite ...")
    if os.path.exists(rec.DB_PATH):
        os.remove(rec.DB_PATH)
    conn = rec.connect()
    rec.load_raw(conn, blotter, custodian, days)
    kpis = rec.run_all_days(conn, days)
    print(kpis[["as_of_date", "new_trades", "stp_trades", "stp_rate", "new_breaks",
                "resolved_today", "open_breaks", "open_4d", "open_5plus"]].to_string(index=False))

    print("\n3) Accuracy - measured against labelled synthetic breaks")
    acc, wt_info = rec.accuracy(conn, truth)
    show = acc.copy()
    for c in ["precision", "recall"]:
        show[c] = show[c].map(lambda v: "n/a" if pd.isna(v) else f"{v:.1%}")
    print(show.drop(columns="engine_category").to_string(index=False))
    print(f"   within-tolerance differences: {wt_info['within_tolerance_labelled']} labelled, "
          f"{wt_info['within_tolerance_flagged']} wrongly flagged")
    print("   (generator and matcher were built together, so real-world performance would be lower)")

    print("\n4) Writing reports ...")
    t = report.load_tables(conn)
    xlsx = report.write_excel(t, acc, wt_info)
    csv, n = report.write_tableau(t)
    print(f"   {xlsx}\n   {csv} ({n} rows)")

    errors, notes = sanity_checks(conn, t, acc, wt_info)
    conn.close()
    print("\nSanity checks:")
    for n_ in notes:
        print("   WARNING:", n_)
    if errors:
        for e in errors:
            print("   ERROR:", e)
        sys.exit(1)
    print("   all hard checks passed")


if __name__ == "__main__":
    main()

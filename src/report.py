"""
report.py - writes reports/recon_report.xlsx and reports/tableau_export.csv.
Every number comes from the SQLite tables built by reconcile.py.
"""
import os

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src import generate_data as gen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "reports")
XLSX_PATH = os.path.join(REPORT_DIR, "recon_report.xlsx")
TABLEAU_PATH = os.path.join(REPORT_DIR, "tableau_export.csv")

RED = PatternFill("solid", fgColor="FFC7CE")
AMBER = PatternFill("solid", fgColor="FFEB9C")
HEADER = PatternFill("solid", fgColor="1F3864")
BUCKETS = ["1 day", "2-3 days", "4 days", "5+ days"]
ACCURACY_NOTE = ("Precision and recall are measured against labelled synthetic breaks. The generator "
                 "and the matcher were built together, so these scores overstate how the engine "
                 "would perform on real custodian data.")


def assumptions():
    days = gen.business_days()
    return pd.DataFrame([
        ("Data", "100% synthetic. Securities (*.SYN tickers, SY-prefix ISINs) and counterparties are fictional; FX pairs are real currency codes with made-up rates."),
        ("Period", f"{len(days)} business days, {days[0]} to {days[-1]}. Weekends skipped; public holidays ignored (none fall in this window in the US or Canada)."),
        ("Volume", f"{gen.TRADES_PER_DAY[0]}-{gen.TRADES_PER_DAY[1]} trades per day, random seed {gen.SEED}."),
        ("Settlement", "Equities and bonds T+1 (North American standard since May 2024). FX spot T+2, except USD/CAD T+1."),
        ("Price tolerance", "Equity/bond: |difference| <= max(1 cent, 0.01% of price). FX: <= 0.01% of the rate. Quantity, side, security and settle date must match exactly."),
        ("STP rate", "Trades of the day matched automatically (exact, or only a within-tolerance price difference) with no break of any kind / all trades of the day."),
        ("Break unit", "One break = one (trade, problem). A trade with two wrong fields counts as two breaks."),
        ("Aging", "Business days since the break was first seen; first seen today = 1 day. Buckets: 1 day, 2-3 days, 4 days (watch list), 5+ days (escalate, shown in red)."),
        ("Resolution", f"Each open break is fixed with probability {gen.P_RESOLVE:.0%} per day (custodian cancel/correct, late instruction or late booking)."),
        ("Injected break rate", f"About {gen.BREAK_RATE:.0%} of trades on average; about {gen.WITHIN_TOL_RATE:.0%} of clean trades get a harmless rounding difference that must NOT be flagged."),
        ("Counterparties", "Counterparty error rates are a DESIGN CHOICE in the generator (some fictional names are made more error-prone on purpose). The By_Counterparty ranking is true by construction - it is not a finding."),
        ("Hard case", "'ref_mismatch' (custodian keyed our reference wrongly) is injected on purpose. The exact-key engine cannot recognise it and reports it as missing at custodian + missing internally."),
        ("Accuracy", ACCURACY_NOTE),
        ("Out of scope", "Holidays, partial settlements, corporate actions, cash/position reconciliation, fees/commissions, CSDR penalties, fuzzy matching."),
    ], columns=["item", "assumption"])


def load_tables(conn):
    q = lambda s: pd.read_sql(s, conn)
    latest = q("SELECT MAX(as_of_date) AS d FROM daily_kpis")["d"][0]
    return {
        "latest": latest,
        "kpis": q("SELECT * FROM daily_kpis ORDER BY as_of_date"),
        "open": q("""SELECT external_ref AS break_id, break_type, counterparty, asset_class,
                            trade_date, settle_date, first_seen, age_bus_days, age_bucket,
                            days_past_settle, detail, suggested_action
                     FROM open_breaks ORDER BY age_bus_days DESC, break_type, external_ref"""),
        "cpty": q("SELECT * FROM kpi_by_counterparty"),
        "type": q("SELECT * FROM kpi_by_type"),
        "asset": q("SELECT * FROM kpi_by_asset_class"),
        "log": q("SELECT * FROM recon_log ORDER BY as_of_date, external_ref, break_type"),
        "totals": q("""SELECT COUNT(DISTINCT external_ref) AS records,
                              (SELECT COUNT(*) FROM (SELECT DISTINCT external_ref, break_type FROM break_history)) AS breaks_raised
                       FROM recon_log"""),
    }


def summary_frame(t):
    k = t["kpis"].iloc[-1]
    allk = t["kpis"]
    rows = [
        ("Report date (latest business day)", t["latest"]),
        ("Trades reconciled today", int(k["new_trades"])),
        ("STP rate today", f"{k['stp_rate']:.2%}"),
        ("Exact-match rate today", f"{k['exact_rate']:.2%}"),
        ("Average STP rate, all days", f"{allk['stp_rate'].mean():.2%}"),
        ("Total records reconciled (all days)", int(t["totals"]["records"][0])),
        ("Breaks raised (all days, trade x type)", int(t["totals"]["breaks_raised"][0])),
        ("New breaks today", int(k["new_breaks"])),
        ("Breaks resolved today", int(k["resolved_today"])),
        ("Open breaks today", int(k["open_breaks"])),
    ]
    return pd.DataFrame(rows, columns=["metric", "value"])


def style_sheet(ws, widths=None):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEADER
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"
    for i, col in enumerate(ws.columns, start=1):
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 70)
    for col, w in (widths or {}).items():
        ws.column_dimensions[col].width = w


def write_excel(t, acc, wt_info, path=XLSX_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    summary = summary_frame(t)
    k = t["kpis"].iloc[-1]
    buckets = pd.DataFrame({"age_bucket": BUCKETS,
                            "open_breaks": [int(k["open_1d"]), int(k["open_2_3d"]),
                                            int(k["open_4d"]), int(k["open_5plus"])]})
    acc_out = acc.copy()
    for c in ["precision", "recall"]:
        acc_out[c] = acc_out[c].round(4)

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        buckets.to_excel(xw, sheet_name="Summary", index=False, startrow=len(summary) + 2)
        t["type"].to_excel(xw, sheet_name="Summary", index=False, startrow=len(summary) + len(buckets) + 5)
        t["open"].to_excel(xw, sheet_name="Open_Breaks", index=False)
        t["kpis"].to_excel(xw, sheet_name="Daily_KPIs", index=False)
        t["cpty"].to_excel(xw, sheet_name="By_Counterparty", index=False)
        t["asset"].to_excel(xw, sheet_name="By_Counterparty", index=False, startrow=len(t["cpty"]) + 4)
        acc_out.to_excel(xw, sheet_name="Accuracy", index=False, startrow=2)
        assumptions().to_excel(xw, sheet_name="Assumptions", index=False)

        wb = xw.book
        for name in wb.sheetnames:
            style_sheet(wb[name])

        ws = wb["Summary"]
        brow = len(summary) + 3                       # header row of the bucket table
        for r in range(brow + 1, brow + 1 + len(buckets)):
            if ws.cell(r, 1).value == "5+ days":
                for c in (1, 2):
                    ws.cell(r, c).fill = RED
            elif ws.cell(r, 1).value == "4 days":
                for c in (1, 2):
                    ws.cell(r, c).fill = AMBER
        for r in (brow, brow + len(buckets) + 3):
            for c in ws[r]:
                if c.value is not None:
                    c.font = Font(bold=True)
        ws.column_dimensions["A"].width = 40

        ws = wb["Open_Breaks"]
        age_col = list(t["open"].columns).index("age_bus_days") + 1
        for r in range(2, ws.max_row + 1):
            age = ws.cell(r, age_col).value
            fill = RED if age >= 5 else AMBER if age == 4 else None
            if fill:
                for c in range(1, ws.max_column + 1):
                    ws.cell(r, c).fill = fill
        ws.auto_filter.ref = ws.dimensions

        ws = wb["By_Counterparty"]
        ws.cell(len(t["cpty"]) + 3, 1).value = ("Note: counterparty error rates are a design choice of the "
                                                "synthetic data generator, so this ranking is by construction, not a finding.")
        ws.cell(len(t["cpty"]) + 3, 1).font = Font(italic=True)

        ws = wb["Accuracy"]
        ws.cell(1, 1).value = ACCURACY_NOTE
        ws.cell(1, 1).font = Font(italic=True)
        ws.cell(2, 1).value = (f"Within-tolerance rounding differences: {wt_info['within_tolerance_labelled']} "
                               f"labelled, {wt_info['within_tolerance_flagged']} wrongly flagged as breaks.")
        for c in ws[3]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = HEADER
        for c in ws[1] + ws[2]:
            if c.column > 1:
                c.fill = PatternFill(fill_type=None)
                c.font = Font()
        ws.freeze_panes = "A4"

        ws = wb["Assumptions"]
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 120
        for row in ws.iter_rows(min_row=2):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    return path


def write_tableau(t, path=TABLEAU_PATH):
    """One tidy table: one row per (business day, record, open break). Matched records have break_type 'none'."""
    log = t["log"].copy()
    log["status"] = log["break_type"].notna().map({True: "Break", False: "Auto-matched"})
    log["break_type"] = log["break_type"].fillna("none")
    log["age_bucket"] = log["age_bucket"].fillna("")
    log["is_new_trade"] = (log["trade_date"] == log["as_of_date"]).astype(int)
    log = log[["as_of_date", "trade_date", "external_ref", "asset_class", "counterparty",
               "match_status", "status", "break_type", "age_bus_days", "age_bucket", "is_new_trade"]]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log.to_csv(path, index=False)
    return path, len(log)

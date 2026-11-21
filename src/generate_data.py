"""
generate_data.py - builds the synthetic data for the reconciliation.

Writes three CSVs to data/raw/:
  internal_blotter.csv  - our books (what the front office booked)
  custodian_file.csv    - what the custodian/broker says, as a stream of NEW / CANCEL messages
  ground_truth.csv      - the answer key: every injected break. The matching SQL never reads it.

Everything is driven by one random seed, so the same seed always gives the same files.
"""
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")

SEED = 42
START_DATE = "2026-09-14"   # Monday. 14-25 Sep 2026 has no US or Canadian market holidays.
N_DAYS = 10                 # business days simulated
TRADES_PER_DAY = (300, 500)
BREAK_RATE = 0.04           # average share of trades that get a break injected
WITHIN_TOL_RATE = 0.03      # share of clean trades given a harmless rounding difference
TWO_ERROR_SHARE = 0.08      # share of field breaks that get a second field error
P_RESOLVE = 0.35            # chance an open break gets fixed on any given day

ASSET_MIX = {"equity": 0.55, "bond": 0.25, "fx": 0.20}

# Break types and how often each is chosen when a trade is picked to break.
# 'ref_mismatch' is a deliberate HARD CASE that is not one of the 7 engine categories:
# the custodian keyed our reference wrongly, so an exact-key engine sees two unrelated
# breaks (missing at custodian + missing internally). It is here to measure that weakness.
BREAK_TYPES = {
    "quantity_mismatch": 0.17,
    "price_mismatch": 0.17,
    "settle_date_mismatch": 0.15,
    "missing_at_custodian": 0.14,
    "missing_internally": 0.12,
    "wrong_side": 0.10,
    "duplicate_at_custodian": 0.12,
    "ref_mismatch": 0.03,
}
FIELD_BREAKS = {"quantity_mismatch": "quantity", "price_mismatch": "price",
                "settle_date_mismatch": "settle_date", "wrong_side": "side"}

# Fictional counterparties. The error weights are a DESIGN CHOICE of this generator
# (some names are made more error-prone on purpose), so any counterparty ranking in the
# report is true by construction - it is not a finding.
COUNTERPARTIES = {
    "Northbridge Securities": 0.6, "Harbourline Capital": 0.7, "Granite Peak Markets": 0.8,
    "Bluewater Brokerage": 0.8, "Cedar Point Trading": 0.9, "Lakeshore Prime": 1.0,
    "Ironwood Partners": 1.0, "Silverfern Securities": 1.1, "Meridian Arc Markets": 1.2,
    "Polaris Street Capital": 1.4, "Kestrel Bay Brokers": 1.8, "Oakhurst Global": 2.5,
}
ACCOUNTS = ["CUST-ACC-001", "CUST-ACC-002", "CUST-ACC-003", "CUST-ACC-004"]

FX_PAIRS = {"EUR/USD": 1.10500, "GBP/USD": 1.31500, "USD/JPY": 147.200,
            "USD/CAD": 1.37800, "AUD/USD": 0.65500}

BLOTTER_COLS = ["trade_id", "external_ref", "trade_date", "settle_date", "asset_class",
                "security_id", "side", "quantity", "price", "currency", "counterparty",
                "custodian_account", "booked_date"]
CUSTODIAN_COLS = ["cust_row_id", "external_ref", "trade_date", "settle_date", "asset_class",
                  "security_id", "side", "quantity", "price", "currency", "counterparty",
                  "custodian_account", "file_date", "msg_type", "cancels_row_id"]


# ---------- calendar ----------
def business_days(start=START_DATE, n=N_DAYS):
    """List of n business days (Mon-Fri) as 'YYYY-MM-DD' strings. Holidays ignored (assumption)."""
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def add_bdays(date_str, n):
    return str(np.busday_offset(np.datetime64(date_str), n, roll="forward"))


def settle_date_for(trade_date, asset_class, security_id):
    """Equities and bonds T+1 (North America since May 2024). FX spot T+2, except USD/CAD T+1."""
    if asset_class == "fx" and security_id != "USD/CAD":
        return add_bdays(trade_date, 2)
    return add_bdays(trade_date, 1)


def price_tolerance(asset_class, price):
    """Same rule as the SQL: 0.01% of price or 1 cent, whichever is larger; FX uses 0.01% only."""
    if asset_class == "fx":
        return 0.0001 * price
    return max(0.01, 0.0001 * price)


# ---------- securities (clearly synthetic) ----------
def isin_check_digit(body):
    """Luhn check digit for an 11-character ISIN body (letters A=10 ... Z=35)."""
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return str((10 - total % 10) % 10)


def make_securities(rng):
    letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    equities = {}
    while len(equities) < 30:
        tkr = "".join(rng.choice(letters, 4)) + ".SYN"
        equities[tkr] = {"base": round(float(rng.uniform(10, 400)), 2),
                         "ccy": "USD" if rng.random() < 0.6 else "CAD"}
    bonds = {}
    for i in range(15):
        body = f"SY{rng.integers(0, 10**9):09d}"
        bonds[body + isin_check_digit(body)] = {"base": round(float(rng.uniform(92, 108)), 3),
                                                 "ccy": "USD" if rng.random() < 0.5 else "CAD"}
    return equities, bonds


def make_trade(rng, trade_date, equities, bonds, cpty_names, cpty_p):
    asset = rng.choice(list(ASSET_MIX), p=list(ASSET_MIX.values()))
    if asset == "equity":
        sec = rng.choice(list(equities))
        qty = int(rng.integers(1, 200)) * 100
        price = round(equities[sec]["base"] * (1 + rng.normal(0, 0.01)), 2)
        ccy = equities[sec]["ccy"]
    elif asset == "bond":
        sec = rng.choice(list(bonds))
        qty = int(rng.integers(1, 50)) * 100_000          # face value
        price = round(bonds[sec]["base"] + rng.normal(0, 0.3), 3)
        ccy = bonds[sec]["ccy"]
    else:
        sec = rng.choice(list(FX_PAIRS))
        qty = int(rng.integers(1, 26)) * 1_000_000         # base-currency notional
        dp = 3 if sec.endswith("JPY") else 5
        price = round(FX_PAIRS[sec] * (1 + rng.normal(0, 0.002)), dp)
        ccy = sec[-3:]                                     # price is in the quote currency
    return {
        "trade_date": trade_date,
        "settle_date": settle_date_for(trade_date, asset, sec),
        "asset_class": str(asset), "security_id": str(sec),
        "side": str(rng.choice(["BUY", "SELL"])), "quantity": qty, "price": float(price),
        "currency": ccy, "counterparty": str(rng.choice(cpty_names, p=cpty_p)),
        "custodian_account": str(rng.choice(ACCOUNTS)),
    }


# ---------- break injection ----------
def corrupt(rng, row, field):
    """Return a copy of a custodian row with one field made wrong (beyond tolerance)."""
    r = dict(row)
    sign = rng.choice([-1, 1])
    if field == "quantity":
        step = {"equity": 100, "bond": 100_000, "fx": 1_000_000}[r["asset_class"]]
        delta = int(rng.integers(1, 10)) * step
        r["quantity"] = r["quantity"] + delta if (sign > 0 or r["quantity"] <= delta) else r["quantity"] - delta
    elif field == "price":
        p, ac = r["price"], r["asset_class"]
        if ac == "equity":
            r["price"] = round(p + sign * max(0.02, round(p * rng.uniform(0.002, 0.02), 2)), 2)
        elif ac == "bond":
            r["price"] = round(p + sign * rng.uniform(0.25, 1.5), 3)
        else:
            dp = 3 if r["security_id"].endswith("JPY") else 5
            r["price"] = round(p * (1 + sign * rng.uniform(0.0005, 0.003)), dp)
        assert abs(r["price"] - p) > price_tolerance(ac, p)
    elif field == "settle_date":
        r["settle_date"] = add_bdays(r["settle_date"], 1)
    elif field == "side":
        r["side"] = "SELL" if r["side"] == "BUY" else "BUY"
    return r


def rounding_noise(rng, row):
    """A small price difference INSIDE tolerance (e.g. custodian reports an extra decimal)."""
    r = dict(row)
    sign = rng.choice([-1, 1])
    step = {"equity": 0.005, "bond": 0.004}.get(r["asset_class"])
    if step is None:
        step = 0.001 if r["security_id"].endswith("JPY") else 0.00001
    r["price"] = round(r["price"] + sign * step, 6)
    assert abs(r["price"] - row["price"]) <= price_tolerance(r["asset_class"], row["price"])
    return r


def generate(seed=SEED, out_dir=RAW_DIR, write=True):
    rng = np.random.default_rng(seed)
    days = business_days()
    equities, bonds = make_securities(rng)
    cpty_names = list(COUNTERPARTIES)
    w = np.array(list(COUNTERPARTIES.values()))
    cpty_p = np.full(len(w), 1 / len(w))
    cpty_break_rate = dict(zip(cpty_names, BREAK_RATE * w / w.mean()))
    btypes, bprobs = list(BREAK_TYPES), list(BREAK_TYPES.values())

    blotter, cust, truth = [], [], []
    seq = 0

    def cust_msg(row, file_date, msg_type="NEW", cancels=None):
        nonlocal seq
        seq += 1
        m = {k: row[k] for k in CUSTODIAN_COLS[1:12]}
        m.update(tmp_id=seq, file_date=file_date, msg_type=msg_type, cancels_tmp=cancels)
        cust.append(m)
        return seq

    trade_no = 0
    for i, day in enumerate(days):
        for _ in range(int(rng.integers(TRADES_PER_DAY[0], TRADES_PER_DAY[1] + 1))):
            trade_no += 1
            t = make_trade(rng, day, equities, bonds, cpty_names, cpty_p)
            t["trade_id"] = f"T{day.replace('-', '')}-{trade_no:05d}"
            t["external_ref"] = f"EXT{2026000000 + trade_no:010d}"
            t["booked_date"] = day

            # When would this break get fixed? Geometric: 1, 2, 3 ... days later.
            fix_idx = i + int(rng.geometric(P_RESOLVE))
            fix_day = days[fix_idx] if fix_idx < N_DAYS else None
            gt = {"external_ref": t["external_ref"], "injected_date": day, "resolved_date": fix_day,
                  "counterparty": t["counterparty"], "asset_class": t["asset_class"]}

            if rng.random() < cpty_break_rate[t["counterparty"]]:
                btype = str(rng.choice(btypes, p=bprobs))
            elif rng.random() < WITHIN_TOL_RATE:
                btype = "within_tolerance"
            else:
                btype = None

            if btype is None:
                blotter.append(t)
                cust_msg(t, day)
            elif btype == "within_tolerance":
                blotter.append(t)
                cust_msg(rounding_noise(rng, t), day)
                truth.append({**gt, "break_type": btype, "resolved_date": None})
            elif btype in FIELD_BREAKS:
                fields = [FIELD_BREAKS[btype]]
                if rng.random() < TWO_ERROR_SHARE:
                    fields.append(str(rng.choice([f for f in FIELD_BREAKS.values() if f != fields[0]])))
                bad = t
                for f in fields:
                    bad = corrupt(rng, bad, f)
                blotter.append(t)
                bad_id = cust_msg(bad, day)
                if fix_day:  # custodian cancels the bad message and re-sends it correctly
                    cust_msg(t, fix_day, "CANCEL", bad_id)
                    cust_msg(t, fix_day)
                inv = {v: k for k, v in FIELD_BREAKS.items()}
                for f in fields:
                    truth.append({**gt, "break_type": inv[f], "two_errors": len(fields) > 1})
            elif btype == "missing_at_custodian":
                blotter.append(t)
                if fix_day:   # instruction finally arrives
                    cust_msg(t, fix_day)
                truth.append({**gt, "break_type": btype})
            elif btype == "missing_internally":
                cust_msg(t, day)
                if fix_day:   # we book it late; if never fixed, it never reaches our books
                    blotter.append({**t, "booked_date": fix_day})
                truth.append({**gt, "break_type": btype})
            elif btype == "duplicate_at_custodian":
                blotter.append(t)
                cust_msg(t, day)
                dup_id = cust_msg(t, day)
                if fix_day:
                    cust_msg(t, fix_day, "CANCEL", dup_id)
                truth.append({**gt, "break_type": btype})
            elif btype == "ref_mismatch":
                blotter.append(t)
                bad = {**t, "external_ref": t["external_ref"][:-2] + t["external_ref"][-1]}  # dropped digit
                bad_id = cust_msg(bad, day)
                if fix_day:
                    cust_msg(bad, fix_day, "CANCEL", bad_id)
                    cust_msg(t, fix_day)
                truth.append({**gt, "break_type": btype, "custodian_ref": bad["external_ref"]})

    blotter = pd.DataFrame(blotter)[BLOTTER_COLS].sort_values(["booked_date", "trade_id"]).reset_index(drop=True)

    # Give custodian messages IDs in the order the custodian sent them (by file date).
    c = pd.DataFrame(cust).sort_values(["file_date", "tmp_id"]).reset_index(drop=True)
    id_map = {tmp: f"C{n + 1:06d}" for n, tmp in enumerate(c["tmp_id"])}
    c["cust_row_id"] = c["tmp_id"].map(id_map)
    c["cancels_row_id"] = c["cancels_tmp"].map(id_map)
    custodian = c[CUSTODIAN_COLS]

    truth = pd.DataFrame(truth).reindex(columns=[
        "external_ref", "break_type", "injected_date", "resolved_date", "counterparty",
        "asset_class", "two_errors", "custodian_ref"])
    truth["two_errors"] = truth["two_errors"].fillna(False).astype(bool)

    if write:
        os.makedirs(out_dir, exist_ok=True)
        blotter.to_csv(os.path.join(out_dir, "internal_blotter.csv"), index=False)
        custodian.to_csv(os.path.join(out_dir, "custodian_file.csv"), index=False)
        truth.to_csv(os.path.join(out_dir, "ground_truth.csv"), index=False)
    return blotter, custodian, truth


if __name__ == "__main__":
    b, c, g = generate()
    print(f"blotter {len(b)} rows, custodian {len(c)} messages, ground truth {len(g)} labels")
    print(g["break_type"].value_counts())

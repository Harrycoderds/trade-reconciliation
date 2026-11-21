"""
Unit tests on tiny hand-built examples.  Run:  python -m unittest discover tests -v
"""
import os
import unittest

import pandas as pd

from src import generate_data as gen
from src import reconcile as rec

DAYS = ["2026-09-14", "2026-09-15", "2026-09-16"]


def trade(ref, **kw):
    """One clean equity trade booked on 2026-09-14 (T+1)."""
    t = {"trade_id": "T" + ref, "external_ref": ref, "trade_date": "2026-09-14",
         "settle_date": "2026-09-15", "asset_class": "equity", "security_id": "ABCD.SYN",
         "side": "BUY", "quantity": 1000, "price": 50.00, "currency": "USD",
         "counterparty": "Test Broker", "custodian_account": "CUST-ACC-001",
         "booked_date": "2026-09-14"}
    t.update(kw)
    return t


def cust(row_id, base, file_date="2026-09-14", msg_type="NEW", cancels=None, **kw):
    c = {k: base[k] for k in gen.CUSTODIAN_COLS[1:12]}
    c.update(cust_row_id=row_id, file_date=file_date, msg_type=msg_type, cancels_row_id=cancels)
    c.update(kw)
    return c


def run(blotter_rows, cust_rows, days=DAYS[:1]):
    conn = rec.connect(":memory:")
    b = pd.DataFrame(blotter_rows, columns=gen.BLOTTER_COLS)
    c = pd.DataFrame(cust_rows, columns=gen.CUSTODIAN_COLS)
    rec.load_raw(conn, b, c, days)
    rec.run_all_days(conn, days)
    return conn


def breaks(conn):
    return sorted(map(tuple, conn.execute(
        "SELECT external_ref, break_type FROM breaks_today").fetchall()))


class TestClassification(unittest.TestCase):

    def check_single(self, cust_kw, expected):
        t = trade("R1")
        conn = run([t], [cust("C1", t, **cust_kw)])
        self.assertEqual(breaks(conn), [("R1", expected)])

    def test_clean_trade_not_flagged(self):
        t = trade("R1")
        conn = run([t], [cust("C1", t)])
        self.assertEqual(breaks(conn), [])
        status = conn.execute("SELECT match_status FROM match_results").fetchone()[0]
        self.assertEqual(status, "EXACT")

    def test_within_tolerance_not_flagged(self):
        cases = [(trade("R1"), 50.005),                               # equity: half a cent
                 (trade("R1", asset_class="fx", security_id="EUR/USD", price=1.10500), 1.10501)]
        for t, cust_price in cases:
            conn = run([t], [cust("C1", t, price=cust_price)])
            self.assertEqual(breaks(conn), [], t["asset_class"])
            status = conn.execute("SELECT match_status FROM match_results").fetchone()[0]
            self.assertEqual(status, "TOLERANCE")

    def test_quantity_mismatch(self):
        self.check_single({"quantity": 1100}, "quantity_mismatch")

    def test_price_mismatch(self):
        self.check_single({"price": 50.02}, "price_mismatch")        # tolerance is 1 cent

    def test_fx_price_beyond_tolerance(self):
        t = trade("R1", asset_class="fx", security_id="EUR/USD", price=1.10500)
        conn = run([t], [cust("C1", t, price=1.10520)])              # 0.018% > 0.01%
        self.assertEqual(breaks(conn), [("R1", "price_mismatch")])

    def test_settle_date_mismatch(self):
        self.check_single({"settle_date": "2026-09-16"}, "settle_date_mismatch")

    def test_wrong_side(self):
        self.check_single({"side": "SELL"}, "wrong_side")

    def test_two_errors_give_two_breaks(self):
        t = trade("R1")
        conn = run([t], [cust("C1", t, quantity=900, side="SELL")])
        self.assertEqual(breaks(conn), [("R1", "quantity_mismatch"), ("R1", "wrong_side")])

    def test_missing_at_custodian(self):
        conn = run([trade("R1")], [])
        self.assertEqual(breaks(conn), [("R1", "missing_at_custodian")])

    def test_missing_internally(self):
        conn = run([], [cust("C1", trade("R9"))])
        self.assertEqual(breaks(conn), [("R9", "missing_internally")])

    def test_duplicate_detected(self):
        t = trade("R1")
        conn = run([t], [cust("C1", t), cust("C2", t)])
        self.assertEqual(breaks(conn), [("R1", "duplicate_at_custodian")])
        rn = conn.execute("SELECT MAX(rn) FROM custodian_asof").fetchone()[0]
        self.assertEqual(rn, 2)

    def test_stp_rate(self):
        ts = [trade(f"R{i}") for i in range(4)]
        cs = [cust(f"C{i}", t) for i, t in enumerate(ts)]
        cs[0]["quantity"] = 5                                         # one break out of four
        conn = run(ts, cs)
        stp = conn.execute("SELECT stp_rate FROM daily_kpis").fetchone()[0]
        self.assertAlmostEqual(stp, 0.75)


class TestAging(unittest.TestCase):

    def test_age_spans_weekend(self):
        # Fri 18, Mon 21, Tue 22 Sep: 3 business days (5 calendar-day span)
        days = ["2026-09-18", "2026-09-21", "2026-09-22"]
        t = trade("R1", trade_date="2026-09-18", settle_date="2026-09-21", booked_date="2026-09-18")
        conn = run([t], [cust("C1", t, file_date="2026-09-18", quantity=1)], days)
        row = conn.execute("SELECT age_bus_days, age_calendar_days, age_bucket, days_past_settle "
                           "FROM open_breaks").fetchone()
        self.assertEqual(row, (3, 4, "2-3 days", 1))

    def test_buckets_4_and_5(self):
        days = gen.business_days()[:5]
        t = trade("R1")
        conn = rec.connect(":memory:")
        rec.load_raw(conn, pd.DataFrame([t]), pd.DataFrame([cust("C1", t, quantity=1)],
                                                             columns=gen.CUSTODIAN_COLS), days)
        got = []
        for d in days:
            rec.run_day(conn, d)
            got.append(conn.execute("SELECT age_bus_days, age_bucket FROM open_breaks").fetchone())
        self.assertEqual(got, [(1, "1 day"), (2, "2-3 days"), (3, "2-3 days"),
                               (4, "4 days"), (5, "5+ days")])

    def test_resolution_closes_break(self):
        t = trade("R1")
        cs = [cust("C1", t, quantity=1),
              cust("C2", t, file_date="2026-09-15", msg_type="CANCEL", cancels="C1"),
              cust("C3", t, file_date="2026-09-15")]
        conn = run([t], cs, DAYS[:2])
        self.assertEqual(breaks(conn), [])
        k = conn.execute("SELECT resolved_today, open_breaks FROM daily_kpis "
                         "WHERE as_of_date = '2026-09-15'").fetchone()
        self.assertEqual(k, (1, 0))


class TestPipeline(unittest.TestCase):

    def test_same_seed_same_data(self):
        a = gen.generate(seed=7, write=False)
        b = gen.generate(seed=7, write=False)
        for x, y in zip(a, b):
            pd.testing.assert_frame_equal(x, y)
        c = gen.generate(seed=8, write=False)
        self.assertFalse(a[0].equals(c[0]))

    def test_same_seed_same_results(self):
        def results():
            b, c, t = gen.generate(seed=7, write=False)
            conn = rec.connect(":memory:")
            rec.load_raw(conn, b, c, gen.business_days())
            k = rec.run_all_days(conn, gen.business_days())
            acc, _ = rec.accuracy(conn, t)
            return k, acc
        k1, a1 = results()
        k2, a2 = results()
        pd.testing.assert_frame_equal(k1, k2)
        pd.testing.assert_frame_equal(a1, a2)

    def test_sql_never_reads_ground_truth(self):
        for f in rec.SQL_FILES:
            with open(os.path.join(rec.SQL_DIR, f)) as fh:
                self.assertNotIn("ground_truth", fh.read())


if __name__ == "__main__":
    unittest.main()

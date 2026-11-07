import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fundamentallab.byma import byma_price_from_row
from fundamentallab.database import connect, create_run
from fundamentallab.models import CompanyContext
from fundamentallab.scanner import scan_argentina


def ctx(symbol="LEDE", price=None):
    return CompanyContext(
        market="ARG",
        symbol=symbol,
        name=symbol,
        quarters=["Q1","Q2","Q3","Q4"],
        revenue=[100,110,120,130],
        net_income=[10,12,15,18],
        cfo=[20,21,22,23],
        capex=[5,5,5,5],
        fcf=[15,16,17,18],
        buybacks=[1,2,3,4],
        diluted_shares=[10,9.9,9.8,9.7],
        price=price,
        currency="ARS",
        metadata={},
    )


class ArgentinaPriceTests(unittest.TestCase):
    def test_byma_uses_previous_close_if_trade_missing(self):
        row = {
            "trade": 0,
            "closingPrice": 0,
            "previousClosingPrice": 123.45,
        }
        price, source = byma_price_from_row(row)
        self.assertEqual(price, 123.45)
        self.assertEqual(source, "BYMA previousClosingPrice")

    def test_byma_prefers_trade(self):
        row = {
            "trade": 150,
            "closingPrice": 149,
            "previousClosingPrice": 148,
        }
        price, source = byma_price_from_row(row)
        self.assertEqual(price, 150)
        self.assertEqual(source, "BYMA trade")

    def test_argentina_scan_does_not_invent_price_when_byma_missing(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            run = create_run(conn, ["ARG"])

            c = ctx(price=None)
            c.metadata["byma_price_status"] = "no_recent_quote"

            results = scan_argentina(conn, run, [c])

            self.assertIsNone(results[0].price)
            self.assertIsNotNone(results[0].intrinsic_value)
            self.assertIsNone(results[0].upside_to_intrinsic)

    def test_existing_byma_price_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            run = create_run(conn, ["ARG"])

            c = ctx(price=200.0)
            c.metadata["byma_price_status"] = "valid_snapshot"

            results = scan_argentina(conn, run, [c])

            self.assertEqual(results[0].price, 200.0)


if __name__ == "__main__":
    unittest.main()

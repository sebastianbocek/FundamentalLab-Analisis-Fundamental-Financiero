import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fundamentallab.byma import (
    byma_snapshot_has_activity,
    validate_argentina_snapshot_prices,
)
from fundamentallab.database import connect, create_run
from fundamentallab.models import CompanyContext
from fundamentallab.scanner import scan_argentina


def make_ctx(symbol="ROSE5", price=None, status="stale"):
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
        metadata={
            "byma_price_status": status,
            "byma_price_source": "test",
        },
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def latest_recent_close(self, symbol, lookback_days=180, max_age_days=30):
        return self.responses[symbol]


class V28BymaPriceValidationTests(unittest.TestCase):
    def test_snapshot_activity(self):
        self.assertTrue(byma_snapshot_has_activity({
            "numberOfOrders": 3,
            "volume": 0,
        }))
        self.assertFalse(byma_snapshot_has_activity({
            "numberOfOrders": 0,
            "volume": 0,
            "tradeVolume": 0,
            "volumeAmount": 0,
        }))

    def test_recent_history_replaces_inactive_snapshot(self):
        rows = [{
            "symbol": "ROSE",
            "trade": 0,
            "closingPrice": 160,
            "numberOfOrders": 0,
            "volume": 0,
            "tradeVolume": 0,
            "volumeAmount": 0,
        }]
        client = FakeClient({
            "ROSE": {
                "price": 162.0,
                "date": "2026-08-28",
                "age_days": 1,
                "status": "recent",
            }
        })

        result = validate_argentina_snapshot_prices(client, rows, max_workers=1)
        self.assertEqual(result["ROSE"]["price"], 162.0)
        self.assertEqual(result["ROSE"]["status"], "valid_history")

    def test_stale_001_is_blocked(self):
        rows = [{
            "symbol": "ROSE5",
            "trade": 0.01,
            "closingPrice": 0.01,
            "numberOfOrders": 0,
            "volume": 0,
            "tradeVolume": 0,
            "volumeAmount": 0,
        }]
        client = FakeClient({
            "ROSE5": {
                "price": None,
                "historical_price": 0.01,
                "date": "2009-02-18",
                "age_days": 6000,
                "status": "stale",
            }
        })

        result = validate_argentina_snapshot_prices(client, rows, max_workers=1)
        self.assertIsNone(result["ROSE5"]["price"])
        self.assertEqual(result["ROSE5"]["status"], "stale")

    def test_stale_price_cannot_create_upside(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            run = create_run(conn, ["ARG"])

            # Defensive scanner: aunque algún caller entregue 0.01,
            # el status stale debe anularlo.
            results = scan_argentina(
                conn,
                run,
                [make_ctx(price=0.01, status="stale")],
            )
            self.assertIsNone(results[0].price)
            self.assertIsNone(results[0].upside_to_intrinsic)

    def test_valid_byma_price_keeps_upside(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            run = create_run(conn, ["ARG"])

            results = scan_argentina(
                conn,
                run,
                [make_ctx(symbol="LEDE", price=850.0, status="valid_snapshot")],
            )
            self.assertEqual(results[0].price, 850.0)
            self.assertIsNotNone(results[0].intrinsic_value)
            self.assertIsNotNone(results[0].upside_to_intrinsic)


if __name__ == "__main__":
    unittest.main()

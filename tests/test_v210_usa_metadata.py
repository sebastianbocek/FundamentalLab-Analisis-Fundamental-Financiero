import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fundamentallab.database import connect, create_run
from fundamentallab.models import CompanyContext
from fundamentallab.scanner import scan_usa
from fundamentallab.engine import evaluate_company


def make_ctx(symbol="TEST"):
    return CompanyContext(
        market="USA",
        symbol=symbol,
        name="Test Corp",
        quarters=["Q1","Q2","Q3","Q4"],
        revenue=[100,110,120,130],
        net_income=[10,12,15,18],
        cfo=[20,22,24,28],
        capex=[5,5,5,5],
        fcf=[15,17,19,23],
        buybacks=[1,2,3,4],
        diluted_shares=[10,9.9,9.8,9.7],
        price=None,
        currency="USD",
        metadata={
            "cik": 1,
            "financial_currency": "USD",
            "market_currency": "USD",
            "dcf_currency_multiplier": 1.0,
        },
    )


class V210USAMetadataTests(unittest.TestCase):
    def test_engine_sets_dcf_status_ok(self):
        c = make_ctx()
        c.price = 10
        result = evaluate_company(c)
        self.assertIsNotNone(result.intrinsic_value)
        self.assertEqual(
            result.metadata.get("dcf_quality_status"),
            "OK",
        )
        self.assertTrue(
            result.metadata.get("dcf_reason")
        )

    @patch("fundamentallab.scanner.usa_yahoo_fresh_quotes")
    def test_scan_usa_stores_price_source_and_date(self, quotes):
        quotes.return_value = {
            "TEST": {
                "price": 123.45,
                "date": "2026-08-28",
                "age_days": 1,
                "currency": "USD",
                "exchange": "NMS",
                "source": "Yahoo chart fresh",
            }
        }

        with patch(
            "fundamentallab.scanner.yfinance_batch_fallback",
            return_value={},
        ):
            with tempfile.TemporaryDirectory() as d:
                conn = connect(Path(d) / "x.sqlite")
                run = create_run(conn, ["USA"])

                results = scan_usa(
                    conn,
                    run,
                    [make_ctx()],
                    sec_client=None,
                )

                metadata = results[0].metadata

                self.assertEqual(
                    metadata.get("price_source_final"),
                    "Yahoo chart fresh",
                )
                self.assertEqual(
                    metadata.get("price_last_date"),
                    "2026-08-28",
                )
                self.assertEqual(
                    metadata.get("financial_currency"),
                    "USD",
                )
                self.assertEqual(
                    metadata.get("dcf_quality_status"),
                    "OK",
                )


if __name__ == "__main__":
    unittest.main()

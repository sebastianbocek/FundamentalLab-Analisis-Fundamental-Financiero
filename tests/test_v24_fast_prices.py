import unittest
from unittest.mock import patch, Mock

from fundamentallab.models import CompanyContext
from fundamentallab.scanner import _needs_price
from fundamentallab.prices import yahoo_chart_prices_concurrent


def make_ctx():
    return CompanyContext(
        market="USA",
        symbol="TEST",
        name="Test",
        quarters=["Q1","Q2","Q3","Q4"],
        revenue=[100,110,120,130],
        net_income=[10,12,15,18],
        cfo=[20,21,22,23],
        capex=[5,5,5,5],
        fcf=[15,16,17,18],
        buybacks=[1,2,3,4],
        diluted_shares=[10,9.9,9.8,9.7],
        price=None,
    )


class FastPriceTests(unittest.TestCase):
    def test_already_priority_needs_price(self):
        ctx = make_ctx()
        needed, _ = _needs_price(ctx, 2)
        self.assertTrue(needed)

    def test_zero_matches_does_not_need_price(self):
        ctx = make_ctx()
        needed, _ = _needs_price(ctx, 0)
        self.assertFalse(needed)

    @patch("fundamentallab.prices.requests.get")
    def test_chart_prices_concurrent(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "chart": {
                "result": [{
                    "meta": {"regularMarketPrice": 123.45},
                    "indicators": {"quote": [{"close": [120, 123.45]}]},
                }]
            }
        }
        get.return_value = response

        prices = yahoo_chart_prices_concurrent(
            ["AAA", "BBB", "CCC"],
            max_workers=3,
            timeout=0.2,
        )
        self.assertEqual(prices["AAA"], 123.45)
        self.assertEqual(prices["BBB"], 123.45)
        self.assertEqual(prices["CCC"], 123.45)


if __name__ == "__main__":
    unittest.main()

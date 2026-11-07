import unittest
from unittest.mock import patch, Mock

from fundamentallab.models import CompanyContext
from fundamentallab.criteria import intrinsic_value_per_share
from fundamentallab.prices import argentina_yahoo_fresh_quotes


def make_ctx(price=1000.0, fx=None, financial_currency=None):
    return CompanyContext(
        market="ARG",
        symbol="TEST",
        name="TEST",
        quarters=["Q1","Q2","Q3","Q4"],
        revenue=[1000,1100,1200,1300],
        net_income=[100,120,150,180],
        cfo=[200,220,240,260],
        capex=[50,50,50,50],
        fcf=[150,170,190,210],
        buybacks=[0,0,0,0],
        diluted_shares=[10,10,10,10],
        price=price,
        currency="ARS",
        metadata={
            "financial_currency": financial_currency,
            "market_currency": "ARS",
            "dcf_currency_multiplier": fx,
        },
    )


class V29Tests(unittest.TestCase):
    def test_dcf_currency_conversion_multiplies_intrinsic(self):
        base = make_ctx(price=None, fx=None, financial_currency="ARS")
        converted = make_ctx(price=None, fx=1000.0, financial_currency="USD")

        i1, _ = intrinsic_value_per_share(base)
        i2, _ = intrinsic_value_per_share(converted)

        self.assertIsNotNone(i1)
        self.assertIsNotNone(i2)
        self.assertAlmostEqual(i2 / i1, 1000.0, places=4)

    def test_missing_fx_rejects_cross_currency_dcf(self):
        c = make_ctx(price=1000, fx=None, financial_currency="USD")
        intrinsic, reason = intrinsic_value_per_share(c)
        self.assertIsNone(intrinsic)
        self.assertIn("FX", reason)

    def test_extreme_intrinsic_price_ratio_rejected(self):
        c = make_ctx(price=0.01, fx=None, financial_currency="ARS")
        intrinsic, reason = intrinsic_value_per_share(c)
        self.assertIsNone(intrinsic)
        self.assertIn("control de calidad", reason)

    @patch("fundamentallab.prices.requests.get")
    def test_fresh_yahoo_arg_quote_is_accepted(self, get):
        response = Mock()
        response.status_code = 200

        import time
        now = int(time.time())

        response.json.return_value = {
            "chart": {
                "result": [{
                    "meta": {
                        "currency": "ARS",
                        "instrumentType": "EQUITY",
                        "symbol": "LEDE.BA",
                    },
                    "timestamp": [now - 86400, now],
                    "indicators": {
                        "quote": [{
                            "close": [800.0, 850.0]
                        }]
                    },
                }]
            }
        }
        get.return_value = response

        quotes = argentina_yahoo_fresh_quotes(["LEDE"], max_age_days=14)
        self.assertEqual(quotes["LEDE"]["price"], 850.0)

    @patch("fundamentallab.prices.requests.get")
    def test_stale_yahoo_arg_quote_is_rejected(self, get):
        response = Mock()
        response.status_code = 200

        import time
        old = int(time.time()) - 86400 * 100

        response.json.return_value = {
            "chart": {
                "result": [{
                    "meta": {
                        "currency": "ARS",
                        "instrumentType": "EQUITY",
                        "symbol": "ROSE5.BA",
                    },
                    "timestamp": [old],
                    "indicators": {
                        "quote": [{
                            "close": [0.01]
                        }]
                    },
                }]
            }
        }
        get.return_value = response

        quotes = argentina_yahoo_fresh_quotes(["ROSE5"], max_age_days=14)
        self.assertNotIn("ROSE5", quotes)


if __name__ == "__main__":
    unittest.main()

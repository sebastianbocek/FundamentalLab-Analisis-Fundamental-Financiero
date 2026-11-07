import unittest
from pathlib import Path


class V212UIRegression(unittest.TestCase):
    def test_ui_contains_argentina_technical_market(self):
        app = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("🇦🇷 Argentina", app)
        self.assertIn("YahooArgentinaMarketDataClient", app)
        self.assertIn("latest_market_watchlist", app)


if __name__ == "__main__":
    unittest.main()

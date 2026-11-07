import unittest
from pathlib import Path


class V210UIRegressionTests(unittest.TestCase):
    def test_metadata_columns_not_arg_only(self):
        app = Path("app.py").read_text(encoding="utf-8")
        self.assertIn(
            "'Fuente precio':metadata.get('price_source_final')",
            app,
        )
        self.assertIn(
            "metadata.get('price_last_date')",
            app,
        )
        self.assertIn(
            "'Moneda fin.':metadata.get('financial_currency')",
            app,
        )
        self.assertIn(
            "'DCF status':metadata.get('dcf_quality_status')",
            app,
        )


if __name__ == "__main__":
    unittest.main()

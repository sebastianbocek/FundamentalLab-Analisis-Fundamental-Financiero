import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from fundamentallab.database import (
    connect,
    create_run,
    finish_run,
    latest_usa_watchlist,
)
from fundamentallab.technical import (
    TechnicalConfig,
    evaluate_technical_conditions,
    macd_components,
    rsi_wilder,
    scan_technical_watchlist,
)


class FakeAlpaca:
    feed = "iex"

    def __init__(self, symbols):
        self.symbols = symbols

    def fetch_bars(self, symbols, timeframe, start, end, batch_size=100, progress=None):
        # Tendencia bajista fuerte -> RSI bajo y MACD% negativo.
        timestamps = pd.date_range(
            "2026-01-01",
            periods=90,
            freq="h",
            tz="UTC",
        )
        close = np.linspace(200.0, 50.0, 90)
        frame = pd.DataFrame({"timestamp": timestamps, "close": close})
        return {str(s): frame.copy() for s in symbols}


class TechnicalIndicatorTests(unittest.TestCase):
    def test_macd_pct_is_scale_invariant(self):
        base = pd.Series(np.linspace(100, 70, 100))
        scaled = base * 10.0

        a = macd_components(base)["macd_pct"].iloc[-1]
        b = macd_components(scaled)["macd_pct"].iloc[-1]

        self.assertAlmostEqual(float(a), float(b), places=10)

    def test_rsi_downtrend_is_oversold(self):
        close = pd.Series(np.linspace(100, 50, 80))
        value = float(rsi_wilder(close).iloc[-1])
        self.assertLess(value, 30.0)

    def test_exact_five_conditions(self):
        cfg = TechnicalConfig(rsi_limit=30.0, macd_pct_limit=-2.4)
        day = {"rsi": 20.0, "macd_pct": -3.0}
        h4 = {"rsi": 25.0, "macd_pct": -2.5}
        h1 = {"rsi": 29.0, "macd_pct": -1.0}

        result = evaluate_technical_conditions(day, h4, h1, cfg)
        self.assertEqual(result["score"], 5)
        self.assertTrue(result["matched"])

    def test_one_failed_condition_is_four_of_five(self):
        cfg = TechnicalConfig(rsi_limit=30.0, macd_pct_limit=-2.4)
        day = {"rsi": 20.0, "macd_pct": -3.0}
        h4 = {"rsi": 25.0, "macd_pct": -1.0}
        h1 = {"rsi": 29.0, "macd_pct": -1.0}

        result = evaluate_technical_conditions(day, h4, h1, cfg)
        self.assertEqual(result["score"], 4)
        self.assertFalse(result["matched"])


class TechnicalDatabaseTests(unittest.TestCase):
    def test_latest_usa_watchlist_prioritizes_surface(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            run = create_run(conn, ["USA"])

            conn.execute(
                """INSERT INTO screen_results(
                     run_id,market,symbol,name,matches,surfaced,price,
                     intrinsic_value,upside_to_intrinsic,criteria_json,metadata_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (run, "USA", "AAA", "AAA", 2, 0, 10, 20, 1.0, "[]", "{}"),
            )
            conn.execute(
                """INSERT INTO screen_results(
                     run_id,market,symbol,name,matches,surfaced,price,
                     intrinsic_value,upside_to_intrinsic,criteria_json,metadata_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (run, "USA", "BBB", "BBB", 3, 1, 10, 15, 0.5, "[]", "{}"),
            )
            conn.commit()
            finish_run(conn, run)

            df = latest_usa_watchlist(conn, limit=10, priority_only=False)
            self.assertEqual(df.iloc[0]["symbol"], "BBB")

    def test_alert_is_only_new_on_entry(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            client = FakeAlpaca(["TEST"])
            cfg = TechnicalConfig(rsi_limit=30.0, macd_pct_limit=-2.4)

            first = scan_technical_watchlist(
                conn,
                ["TEST"],
                client,
                config=cfg,
            )
            second = scan_technical_watchlist(
                conn,
                ["TEST"],
                client,
                config=cfg,
            )

            self.assertTrue(first[0]["matched"])
            self.assertTrue(first[0]["alert_triggered"])
            self.assertTrue(second[0]["matched"])
            self.assertFalse(second[0]["alert_triggered"])


if __name__ == "__main__":
    unittest.main()

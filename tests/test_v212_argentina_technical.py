import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from fundamentallab.database import (
    connect,
    create_run,
    finish_run,
    latest_market_watchlist,
    update_technical_alert_state,
)
from fundamentallab.technical import (
    TechnicalConfig,
    YahooArgentinaMarketDataClient,
    _resample_arg_session_4h,
    latest_indicator_snapshot,
)


class V212ArgentinaTechnicalTests(unittest.TestCase):
    def test_yahoo_symbol_mapping(self):
        self.assertEqual(
            YahooArgentinaMarketDataClient.yahoo_symbol("GGAL"),
            "GGAL.BA",
        )
        self.assertEqual(
            YahooArgentinaMarketDataClient.yahoo_symbol("GGAL.BA"),
            "GGAL.BA",
        )

    def test_arg_watchlist_uses_latest_arg_run(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            run = create_run(conn, ["ARG"])
            conn.execute(
                """INSERT INTO screen_results(
                     run_id,market,symbol,name,matches,surfaced,price,
                     intrinsic_value,upside_to_intrinsic,criteria_json,metadata_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (run, "ARG", "GGAL", "GGAL", 3, 1, 100, 140, 0.4, "[]", "{}"),
            )
            conn.commit()
            finish_run(conn, run)

            df = latest_market_watchlist(conn, "ARG", limit=10)
            self.assertEqual(df.iloc[0]["symbol"], "GGAL")

    def test_alert_state_is_separate_by_market(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            self.assertTrue(update_technical_alert_state(conn, "USA", "ABC", True))
            self.assertFalse(update_technical_alert_state(conn, "USA", "ABC", True))
            self.assertTrue(update_technical_alert_state(conn, "ARG", "ABC", True))
            self.assertFalse(update_technical_alert_state(conn, "ARG", "ABC", True))

    def test_arg_low_liquidity_becomes_nd(self):
        now = datetime.now(timezone.utc)
        timestamps = pd.date_range(
            end=now - timedelta(hours=2),
            periods=90,
            freq="h",
            tz="UTC",
        )
        frame = pd.DataFrame({
            "timestamp": timestamps,
            "close": np.linspace(100, 70, 90),
            "volume": [0.0] * 80 + [100.0] * 10,
        })

        snap = latest_indicator_snapshot(
            frame,
            TechnicalConfig(arg_min_nonzero_volume_ratio=0.35),
            market="ARG",
            timeframe="1H",
        )
        self.assertIsNone(snap["rsi"])
        self.assertEqual(snap["quality"]["status"], "low_liquidity")

    def test_arg_stale_becomes_nd(self):
        end = datetime.now(timezone.utc) - timedelta(days=20)
        timestamps = pd.date_range(end=end, periods=90, freq="h", tz="UTC")
        frame = pd.DataFrame({
            "timestamp": timestamps,
            "close": np.linspace(100, 70, 90),
            "volume": [100.0] * 90,
        })
        snap = latest_indicator_snapshot(
            frame,
            TechnicalConfig(arg_max_stale_days=7),
            market="ARG",
            timeframe="1H",
        )
        self.assertIsNone(snap["rsi"])
        self.assertEqual(snap["quality"]["status"], "stale")

    def test_session_resample_builds_4h_bars(self):
        # Two prior sessions, 6 hourly bars each -> two 4H-ish blocks per day.
        base = pd.Timestamp("2026-09-10 14:00:00", tz="UTC")
        rows = []
        for day_offset in (0, 1):
            start = base + pd.Timedelta(days=day_offset)
            for i in range(6):
                rows.append({
                    "timestamp": start + pd.Timedelta(hours=i),
                    "close": 100 + day_offset * 10 + i,
                    "volume": 1000,
                })
        hourly = pd.DataFrame(rows)
        effective_end = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
        h4 = _resample_arg_session_4h(hourly, effective_end)
        self.assertEqual(len(h4), 4)
        self.assertEqual(float(h4.iloc[0]["close"]), 103.0)
        self.assertEqual(float(h4.iloc[1]["close"]), 105.0)


    def test_yahoo_chart_parser_with_mock_session(self):
        class Response:
            status_code = 200
            def raise_for_status(self):
                return None
            def json(self):
                return {
                    "chart": {
                        "result": [{
                            "timestamp": [1700000000, 1700003600],
                            "indicators": {
                                "quote": [{
                                    "close": [100.0, 98.0],
                                    "volume": [1000, 2000],
                                }]
                            },
                        }]
                    }
                }

        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                return Response()

        client = YahooArgentinaMarketDataClient(session=Session())
        end = datetime(2026, 9, 14, tzinfo=timezone.utc)
        frame = client._chart(
            "GGAL",
            "1h",
            end - timedelta(days=2),
            end,
        )
        self.assertEqual(len(frame), 2)
        self.assertEqual(float(frame.iloc[-1]["close"]), 98.0)
        self.assertEqual(float(frame.iloc[-1]["volume"]), 2000.0)

    def test_migration_from_v211_adds_market_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite"
            old = sqlite3.connect(path)
            old.executescript(
                """
                CREATE TABLE technical_scan_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT,
                  finished_at TEXT, source TEXT, ticker_count INTEGER,
                  rsi_limit REAL, macd_pct_limit REAL);
                CREATE TABLE technical_results (
                  scan_id INTEGER NOT NULL, symbol TEXT NOT NULL, price REAL,
                  rsi_1d REAL, macd_pct_1d REAL, rsi_4h REAL, macd_pct_4h REAL,
                  rsi_1h REAL, macd_pct_1h REAL, score INTEGER, evaluable INTEGER,
                  matched INTEGER, alert_triggered INTEGER, conditions_json TEXT,
                  bar_1d TEXT, bar_4h TEXT, bar_1h TEXT,
                  bars_1d INTEGER, bars_4h INTEGER, bars_1h INTEGER, source TEXT,
                  PRIMARY KEY(scan_id,symbol));
                """
            )
            old.commit()
            old.close()

            conn = connect(path)
            run_cols = {r[1] for r in conn.execute("PRAGMA table_info(technical_scan_runs)")}
            result_cols = {r[1] for r in conn.execute("PRAGMA table_info(technical_results)")}
            self.assertIn("market", run_cols)
            self.assertIn("market", result_cols)
            self.assertIn("quality_json", result_cols)


if __name__ == "__main__":
    unittest.main()

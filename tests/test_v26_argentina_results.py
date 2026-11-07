import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from fundamentallab.byma import _empty_arg_context, build_argentina_contexts
from fundamentallab.database import (
    connect, create_run, finish_run, latest_results, store_result
)
from fundamentallab.engine import evaluate_company


class FakeByma:
    def refresh_universe(self, conn):
        return pd.DataFrame([
            {"symbol":"AAA","securityDesc":"AAA SA","trade":100.0,"numberOfOrders":1},
            {"symbol":"BBB","securityDesc":"BBB SA","trade":200.0,"numberOfOrders":1},
        ])


class ArgentinaResultsTests(unittest.TestCase):
    def test_placeholder_context_is_arg(self):
        c = _empty_arg_context("AAA", 100.0, "AAA SA", "sin datos", "AAA.BA")
        self.assertEqual(c.market, "ARG")
        self.assertEqual(c.symbol, "AAA")
        self.assertEqual(c.price, 100.0)
        self.assertEqual(c.metadata["fundamentals_status"], "unavailable")

    @patch("fundamentallab.byma.yahoo_arg_context")
    def test_all_byma_symbols_survive_even_without_fundamentals(self, mocked):
        mocked.side_effect = lambda symbol, price, name: _empty_arg_context(
            symbol, price, name, "sin datos", symbol + ".BA"
        )
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")
            contexts, universe = build_argentina_contexts(
                conn, FakeByma(), max_workers=2
            )
            self.assertEqual(len(universe), 2)
            self.assertEqual(len(contexts), 2)
            self.assertEqual({c.symbol for c in contexts}, {"AAA","BBB"})

    def test_latest_results_can_filter_arg(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / "x.sqlite")

            run_usa = create_run(conn, ["USA"])
            usa_ctx = _empty_arg_context("USX", 10, "US X", "x")
            usa_ctx.market = "USA"
            usa_res = evaluate_company(usa_ctx)
            store_result(conn, run_usa, usa_res)
            finish_run(conn, run_usa)

            run_arg = create_run(conn, ["ARG"])
            arg_ctx = _empty_arg_context("ARGX", 20, "ARG X", "x")
            arg_res = evaluate_company(arg_ctx)
            store_result(conn, run_arg, arg_res)
            finish_run(conn, run_arg)

            both = latest_results(conn)
            arg = latest_results(conn, "ARG")
            usa = latest_results(conn, "USA")

            self.assertEqual(set(both["market"]), {"USA","ARG"})
            self.assertEqual(set(arg["market"]), {"ARG"})
            self.assertEqual(set(usa["market"]), {"USA"})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import time

from fundamentallab.database import connect, latest_market_watchlist
from fundamentallab.technical import (
    AlpacaMarketDataClient,
    YahooArgentinaMarketDataClient,
    TechnicalConfig,
    scan_technical_watchlist,
)


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "fundamentallab.sqlite"
CONFIG_PATH = ROOT / "data" / "technical_monitor.json"


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No existe {CONFIG_PATH}. Copiá technical_monitor.example.json "
            "como data/technical_monitor.json y completá la configuración."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def run_once(config):
    conn = connect(DB_PATH)
    market = str(config.get("market", "USA")).upper().strip()
    if market not in {"USA", "ARG"}:
        raise ValueError("market debe ser USA o ARG")

    watch = latest_market_watchlist(
        conn,
        market=market,
        limit=int(config.get("ticker_limit", 100)),
        priority_only=bool(config.get("priority_only", False)),
    )

    if watch.empty:
        print(f"No hay corrida {market} terminada. Ejecutá el scanner fundamental primero.")
        return

    if market == "USA":
        client = AlpacaMarketDataClient(
            config.get("alpaca_key_id", ""),
            config.get("alpaca_secret_key", ""),
            feed=config.get("feed", "iex"),
        )
    else:
        client = YahooArgentinaMarketDataClient(
            max_workers=int(config.get("arg_workers", 10)),
        )

    rules = TechnicalConfig(
        rsi_limit=float(config.get("rsi_limit", 30.0)),
        macd_pct_limit=float(config.get("macd_pct_limit", -2.4)),
        arg_max_stale_days=int(config.get("arg_max_stale_days", 7)),
        arg_min_nonzero_volume_ratio=float(
            config.get("arg_min_nonzero_volume_ratio", 0.35)
        ),
    )

    def progress(stage, done, total, message):
        print(f"[{stage}] {message}")

    results = scan_technical_watchlist(
        conn,
        watch["symbol"].astype(str).tolist(),
        client,
        config=rules,
        telegram_bot_token=config.get("telegram_bot_token") or None,
        telegram_chat_id=config.get("telegram_chat_id") or None,
        progress=progress,
    )

    active = [r for r in results if r["matched"]]
    new_alerts = [r for r in results if r["alert_triggered"]]
    evaluable = [r for r in results if r["evaluable"]]

    print(
        f"{market} terminado: {len(results)} tickers | "
        f"cobertura 5/5={len(evaluable)} | "
        f"5/5 activos={len(active)} | alertas nuevas={len(new_alerts)}"
    )


def main():
    config = load_config()
    interval = max(5, int(config.get("interval_minutes", 15)))
    market = str(config.get("market", "USA")).upper()

    print("=" * 70)
    print("FundamentalLab V2.12 — Multi-Market Technical Monitor")
    print(f"Mercado: {market}")
    print(f"DB: {DB_PATH}")
    print(f"Intervalo: {interval} minutos")
    print("CTRL+C para detener")
    print("=" * 70)

    while True:
        try:
            run_once(config)
        except KeyboardInterrupt:
            print("Monitor detenido.")
            break
        except Exception as exc:
            print(f"ERROR: {exc}")

        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("Monitor detenido.")
            break


if __name__ == "__main__":
    main()

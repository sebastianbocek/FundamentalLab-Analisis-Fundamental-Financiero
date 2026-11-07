from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo
import math
from typing import Callable, Iterable

import numpy as np
import pandas as pd
import requests

from .database import (
    create_technical_run,
    finish_technical_run,
    store_technical_result,
    update_technical_alert_state,
)


ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
YAHOO_CHART_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)

NEW_YORK = ZoneInfo("America/New_York")
BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")

DEFAULT_RSI_LIMIT = 30.0
# -2.4% es un punto de partida normalizado; no equivale matemáticamente a -24.
DEFAULT_MACD_PCT_LIMIT = -2.4

RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ProgressFn = Callable[[str, int, int, str], None] | None


@dataclass(frozen=True)
class TechnicalConfig:
    rsi_limit: float = DEFAULT_RSI_LIMIT
    macd_pct_limit: float = DEFAULT_MACD_PCT_LIMIT
    rsi_period: int = RSI_PERIOD
    macd_fast: int = MACD_FAST
    macd_slow: int = MACD_SLOW
    macd_signal: int = MACD_SIGNAL

    # Calidad mínima general.
    min_bars: int = 50

    # Argentina: evitar falsas señales en especies ilíquidas/stale.
    arg_max_stale_days: int = 7
    arg_min_nonzero_volume_ratio: float = 0.35


class AlpacaMarketDataClient:
    """Historical Stock Bars USA — batch multi-symbol."""

    market = "USA"

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        feed: str = "iex",
        timeout: int = 30,
        session: requests.Session | None = None,
    ):
        self.key_id = str(key_id or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.feed = str(feed or "iex").strip().lower()
        self.timeout = int(timeout)
        self.session = session or requests.Session()

        if not self.key_id or not self.secret_key:
            raise ValueError("Faltan APCA-API-KEY-ID / APCA-API-SECRET-KEY.")
        if self.feed not in {"iex", "sip"}:
            raise ValueError("feed debe ser 'iex' o 'sip'.")

        self.session.headers.update({
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
            "User-Agent": "FundamentalLab/2.12 MultiMarketTechnicals",
        })

    @property
    def source_name(self):
        return f"Alpaca {self.feed}"

    def fetch_bars(
        self,
        symbols: Iterable[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        batch_size: int = 100,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, pd.DataFrame]:
        symbols = _clean_symbols(symbols)
        if not symbols:
            return {}

        out: dict[str, list[dict]] = {s: [] for s in symbols}
        batches = [
            symbols[i:i + max(1, int(batch_size))]
            for i in range(0, len(symbols), max(1, int(batch_size)))
        ]

        for completed, batch in enumerate(batches, start=1):
            page_token = None
            while True:
                params = {
                    "symbols": ",".join(batch),
                    "timeframe": timeframe,
                    "start": _rfc3339(start),
                    "end": _rfc3339(end),
                    "limit": 10000,
                    "adjustment": "all",
                    "feed": self.feed,
                    "sort": "asc",
                }
                if page_token:
                    params["page_token"] = page_token

                response = self.session.get(
                    ALPACA_BARS_URL,
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code == 403 and self.feed == "sip":
                    raise RuntimeError(
                        "Alpaca rechazó feed=SIP. Probá IEX o verificá tu plan."
                    )
                if response.status_code == 429:
                    raise RuntimeError("Alpaca respondió 429 (rate limit).")
                response.raise_for_status()
                payload = response.json()

                for symbol, rows in (payload.get("bars") or {}).items():
                    symbol = str(symbol).upper()
                    if symbol in out:
                        out[symbol].extend(rows or [])

                page_token = payload.get("next_page_token")
                if not page_token:
                    break

            if progress:
                progress(completed, len(batches), f"{timeframe}: batch {completed}/{len(batches)}")

        frames = {}
        for symbol, rows in out.items():
            frames[symbol] = _alpaca_rows_to_frame(rows)
            frames[symbol] = _closed_bars_only_usa(frames[symbol], timeframe, end)
        return frames

    def fetch_datasets(
        self,
        symbols: Iterable[str],
        progress: ProgressFn = None,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        symbols = _clean_symbols(symbols)
        now = datetime.now(timezone.utc)
        effective_end = now - timedelta(minutes=20)
        plans = {
            "1Day": timedelta(days=260),
            "4Hour": timedelta(days=120),
            "1Hour": timedelta(days=45),
        }
        datasets = {}

        for stage_index, (timeframe, lookback) in enumerate(plans.items(), start=1):
            if progress:
                progress(
                    "download",
                    stage_index - 1,
                    len(plans),
                    f"USA · descargando {timeframe} para {len(symbols)} tickers...",
                )

            def batch_progress(done, total, message):
                if progress:
                    progress("download", stage_index - 1, len(plans), message)

            datasets[timeframe] = self.fetch_bars(
                symbols,
                timeframe=timeframe,
                start=effective_end - lookback,
                end=effective_end,
                batch_size=100,
                progress=batch_progress,
            )
        return datasets


class YahooArgentinaMarketDataClient:
    """
    Provider gratuito inicial para Argentina.

    - Convierte GGAL -> GGAL.BA.
    - Hace requests HTTP al endpoint chart de Yahoo; no abre navegador.
    - Descarga 1D + 1H y construye 4H localmente desde la sesión.
    - No necesita API key.
    """

    market = "ARG"
    source_name = "Yahoo .BA"

    def __init__(
        self,
        timeout: int = 12,
        max_workers: int = 10,
        session: requests.Session | None = None,
    ):
        self.timeout = int(timeout)
        self.max_workers = max(1, min(int(max_workers), 16))
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/152 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        })

    @staticmethod
    def yahoo_symbol(symbol: str) -> str:
        symbol = str(symbol or "").strip().upper()
        return symbol if symbol.endswith(".BA") else f"{symbol}.BA"

    def _chart(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        ysym = self.yahoo_symbol(symbol)
        params = {
            "period1": int(_ensure_utc(start).timestamp()),
            "period2": int(_ensure_utc(end).timestamp()),
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }

        last_error = None
        for host in YAHOO_CHART_HOSTS:
            try:
                response = self.session.get(
                    f"{host}/v8/finance/chart/{ysym}",
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code in {404, 422}:
                    continue
                if response.status_code == 429:
                    last_error = RuntimeError("Yahoo respondió 429 (rate limit).")
                    continue
                response.raise_for_status()
                result = (response.json().get("chart", {}).get("result") or [])
                if not result:
                    continue

                payload = result[0]
                timestamps = payload.get("timestamp") or []
                quotes = payload.get("indicators", {}).get("quote") or []
                quote = quotes[0] if quotes else {}
                closes = quote.get("close") or []
                volumes = quote.get("volume") or []

                rows = []
                for i, ts in enumerate(timestamps):
                    close = closes[i] if i < len(closes) else None
                    volume = volumes[i] if i < len(volumes) else None
                    try:
                        close = float(close)
                        if not math.isfinite(close) or close <= 0:
                            continue
                        rows.append({
                            "timestamp": pd.to_datetime(int(ts), unit="s", utc=True),
                            "close": close,
                            "volume": _finite_or_zero(volume),
                        })
                    except Exception:
                        continue

                if not rows:
                    return _empty_frame()

                return (
                    pd.DataFrame(rows)
                    .drop_duplicates(subset=["timestamp"], keep="last")
                    .sort_values("timestamp")
                    .reset_index(drop=True)
                )
            except Exception as exc:
                last_error = exc
                continue

        if last_error and "429" in str(last_error):
            raise last_error
        return _empty_frame()

    def _fetch_symbol_bundle(self, symbol: str, effective_end: datetime):
        daily = self._chart(
            symbol,
            "1d",
            effective_end - timedelta(days=420),
            effective_end,
        )
        hourly = self._chart(
            symbol,
            "1h",
            effective_end - timedelta(days=180),
            effective_end,
        )

        daily = _closed_bars_only_arg(daily, "1Day", effective_end)
        hourly = _closed_bars_only_arg(hourly, "1Hour", effective_end)
        h4 = _resample_arg_session_4h(hourly, effective_end)
        return daily, h4, hourly

    def fetch_datasets(
        self,
        symbols: Iterable[str],
        progress: ProgressFn = None,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        symbols = _clean_symbols(symbols)
        datasets = {
            "1Day": {s: _empty_frame() for s in symbols},
            "4Hour": {s: _empty_frame() for s in symbols},
            "1Hour": {s: _empty_frame() for s in symbols},
        }
        if not symbols:
            return datasets

        # Margen conservador para no leer la vela intrahoraria todavía abierta.
        effective_end = datetime.now(timezone.utc) - timedelta(minutes=20)
        done = 0

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(symbols))) as executor:
            futures = {
                executor.submit(self._fetch_symbol_bundle, symbol, effective_end): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    daily, h4, hourly = future.result()
                except Exception:
                    daily, h4, hourly = _empty_frame(), _empty_frame(), _empty_frame()

                datasets["1Day"][symbol] = daily
                datasets["4Hour"][symbol] = h4
                datasets["1Hour"][symbol] = hourly
                done += 1

                if progress and (done == len(symbols) or done % 10 == 0):
                    progress(
                        "download",
                        done,
                        len(symbols),
                        f"Argentina · Yahoo .BA {done}/{len(symbols)} tickers",
                    )

        return datasets


# ============================================================================
# INDICADORES
# ============================================================================

def rsi_wilder(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI con suavizado de Wilder (alpha=1/period)."""
    close = pd.to_numeric(close, errors="coerce").astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(
        alpha=1.0 / float(period),
        adjust=False,
        min_periods=period,
    ).mean()
    avg_loss = loss.ewm(
        alpha=1.0 / float(period),
        adjust=False,
        min_periods=period,
    ).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return rsi


def macd_components(
    close: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
):
    """MACD estándar + MACD% = MACD line / precio × 100."""
    close = pd.to_numeric(close, errors="coerce").astype(float)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    macd_pct = (macd_line / close.replace(0.0, np.nan)) * 100.0

    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
        "macd_pct": macd_pct,
    })


def latest_indicator_snapshot(
    df: pd.DataFrame,
    config: TechnicalConfig,
    *,
    market: str = "USA",
    timeframe: str = "",
) -> dict:
    min_bars = max(
        int(config.min_bars),
        config.rsi_period + 5,
        config.macd_slow + config.macd_signal + 5,
    )

    quality = {
        "status": "ok",
        "reason": "",
        "nonzero_volume_ratio": None,
        "stale_days": None,
    }

    if df is None or len(df) < min_bars:
        quality.update({
            "status": "insufficient_bars",
            "reason": f"Sólo {0 if df is None else len(df)} barras; mínimo {min_bars}.",
        })
        return _empty_snapshot(df, quality)

    work = df.copy()
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["timestamp", "close"]).reset_index(drop=True)
    if len(work) < min_bars:
        quality.update({
            "status": "insufficient_bars",
            "reason": f"Sólo {len(work)} barras válidas; mínimo {min_bars}.",
        })
        return _empty_snapshot(work, quality)

    if str(market).upper() == "ARG":
        # Stale guard: 7 días permite fines de semana/feriados sin bloquear acciones líquidas.
        try:
            last_ts = pd.Timestamp(work["timestamp"].iloc[-1])
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            stale_days = (
                datetime.now(timezone.utc) - last_ts.to_pydatetime().astimezone(timezone.utc)
            ).total_seconds() / 86400.0
            quality["stale_days"] = stale_days
            if stale_days > float(config.arg_max_stale_days):
                quality.update({
                    "status": "stale",
                    "reason": f"Última vela hace {stale_days:.1f} días.",
                })
                return _empty_snapshot(work, quality)
        except Exception:
            pass

        # Liquidity guard sólo si el provider entregó volumen.
        if "volume" in work.columns:
            recent = pd.to_numeric(
                work["volume"].tail(min(60, len(work))), errors="coerce"
            ).fillna(0.0)
            if len(recent):
                ratio = float((recent > 0).mean())
                quality["nonzero_volume_ratio"] = ratio
                if ratio < float(config.arg_min_nonzero_volume_ratio):
                    quality.update({
                        "status": "low_liquidity",
                        "reason": (
                            f"Sólo {ratio:.0%} de las velas recientes tienen volumen; "
                            f"mínimo {config.arg_min_nonzero_volume_ratio:.0%}."
                        ),
                    })
                    return _empty_snapshot(work, quality)

    close = work["close"].astype(float)
    rsi = rsi_wilder(close, config.rsi_period)
    macd = macd_components(close, config.macd_fast, config.macd_slow, config.macd_signal)
    idx = len(work) - 1

    def val(series):
        try:
            x = float(series.iloc[idx])
            return x if math.isfinite(x) else None
        except Exception:
            return None

    return {
        "rsi": val(rsi),
        "macd": val(macd["macd"]),
        "macd_signal": val(macd["signal"]),
        "macd_histogram": val(macd["histogram"]),
        "macd_pct": val(macd["macd_pct"]),
        "price": val(close),
        "bar_time": _iso_or_none(work["timestamp"].iloc[-1]),
        "bars": int(len(work)),
        "quality": quality,
        "timeframe": timeframe,
    }


def evaluate_technical_conditions(day: dict, h4: dict, h1: dict, config: TechnicalConfig) -> dict:
    conditions = {
        "day_rsi": _lt(day.get("rsi"), config.rsi_limit),
        "day_macd_pct": _lt(day.get("macd_pct"), config.macd_pct_limit),
        "h4_rsi": _lt(h4.get("rsi"), config.rsi_limit),
        "h4_macd_pct": _lt(h4.get("macd_pct"), config.macd_pct_limit),
        "h1_rsi": _lt(h1.get("rsi"), config.rsi_limit),
    }
    evaluable = all(v is not None for v in conditions.values())
    score = sum(1 for v in conditions.values() if v is True)
    matched = evaluable and score == 5
    return {
        "conditions": conditions,
        "score": score,
        "evaluable": evaluable,
        "matched": matched,
    }


# ============================================================================
# SCANNER MULTI-MERCADO
# ============================================================================

def scan_technical_watchlist(
    conn,
    symbols: Iterable[str],
    client,
    config: TechnicalConfig | None = None,
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
    progress: ProgressFn = None,
):
    config = config or TechnicalConfig()
    symbols = _clean_symbols(symbols)
    if not symbols:
        return []

    market = str(getattr(client, "market", "USA") or "USA").upper().strip()
    source = str(
        getattr(client, "source_name", None)
        or (f"Alpaca {getattr(client, 'feed', 'iex')}" if market == "USA" else "Market Data")
    )

    run_id = create_technical_run(
        conn,
        source=source,
        ticker_count=len(symbols),
        rsi_limit=config.rsi_limit,
        macd_pct_limit=config.macd_pct_limit,
        market=market,
    )

    # V2.12 providers pueden descargar los tres datasets optimizadamente.
    if hasattr(client, "fetch_datasets"):
        datasets = client.fetch_datasets(symbols, progress=progress)
    else:
        # Compatibilidad con FakeAlpaca/tests y clients V2.11.
        now = datetime.now(timezone.utc)
        effective_end = now - timedelta(minutes=20)
        plans = {
            "1Day": timedelta(days=260),
            "4Hour": timedelta(days=120),
            "1Hour": timedelta(days=45),
        }
        datasets = {}
        for stage_index, (timeframe, lookback) in enumerate(plans.items(), start=1):
            if progress:
                progress(
                    "download",
                    stage_index - 1,
                    len(plans),
                    f"Descargando {timeframe} para {len(symbols)} tickers...",
                )
            datasets[timeframe] = client.fetch_bars(
                symbols,
                timeframe=timeframe,
                start=effective_end - lookback,
                end=effective_end,
                batch_size=100,
                progress=None,
            )

    results = []
    new_alerts = []

    for idx, symbol in enumerate(symbols, start=1):
        day = latest_indicator_snapshot(
            datasets.get("1Day", {}).get(symbol), config, market=market, timeframe="1D"
        )
        h4 = latest_indicator_snapshot(
            datasets.get("4Hour", {}).get(symbol), config, market=market, timeframe="4H"
        )
        h1 = latest_indicator_snapshot(
            datasets.get("1Hour", {}).get(symbol), config, market=market, timeframe="1H"
        )

        verdict = evaluate_technical_conditions(day, h4, h1, config)
        alert_triggered = update_technical_alert_state(
            conn, market, symbol, verdict["matched"]
        )

        price = h1.get("price") or h4.get("price") or day.get("price")
        quality = {
            "1D": day.get("quality") or {},
            "4H": h4.get("quality") or {},
            "1H": h1.get("quality") or {},
        }

        row = {
            "market": market,
            "symbol": symbol,
            "price": price,
            "rsi_1d": day.get("rsi"),
            "macd_pct_1d": day.get("macd_pct"),
            "rsi_4h": h4.get("rsi"),
            "macd_pct_4h": h4.get("macd_pct"),
            "rsi_1h": h1.get("rsi"),
            "macd_pct_1h": h1.get("macd_pct"),
            "score": verdict["score"],
            "evaluable": verdict["evaluable"],
            "matched": verdict["matched"],
            "alert_triggered": alert_triggered,
            "conditions": verdict["conditions"],
            "bar_1d": day.get("bar_time"),
            "bar_4h": h4.get("bar_time"),
            "bar_1h": h1.get("bar_time"),
            "bars_1d": day.get("bars"),
            "bars_4h": h4.get("bars"),
            "bars_1h": h1.get("bars"),
            "source": source,
            "quality": quality,
        }

        store_technical_result(conn, run_id, row)
        results.append(row)
        if alert_triggered:
            new_alerts.append(row)

        if progress and (idx == len(symbols) or idx % 20 == 0):
            progress(
                "evaluate",
                idx,
                len(symbols),
                (
                    f"{market} técnicos {idx}/{len(symbols)} · "
                    f"5/5 activos: {sum(r['matched'] for r in results)} · "
                    f"alertas nuevas: {len(new_alerts)}"
                ),
            )

    finish_technical_run(conn, run_id)
    conn.commit()

    if telegram_bot_token and telegram_chat_id:
        for row in new_alerts:
            send_telegram_message(
                telegram_bot_token,
                telegram_chat_id,
                format_alert_message(row, config),
            )

    return results


def format_alert_message(row: dict, config: TechnicalConfig) -> str:
    market = str(row.get("market") or "USA").upper()
    flag = "🇦🇷" if market == "ARG" else "🇺🇸"
    currency = "ARS" if market == "ARG" else "USD"
    return (
        "🚨 FUNDAMENTALLAB — ALERTA TÉCNICA 5/5\n\n"
        f"{flag} Mercado: {market}\n"
        f"Ticker: {row['symbol']}\n"
        f"Precio: {currency} {_fmt(row.get('price'), 2)}\n\n"
        "1D\n"
        f"RSI: {_fmt(row.get('rsi_1d'), 2)}  (< {config.rsi_limit:g}) ✅\n"
        f"MACD%: {_fmt(row.get('macd_pct_1d'), 2)}%  (< {config.macd_pct_limit:g}%) ✅\n\n"
        "4H\n"
        f"RSI: {_fmt(row.get('rsi_4h'), 2)}  (< {config.rsi_limit:g}) ✅\n"
        f"MACD%: {_fmt(row.get('macd_pct_4h'), 2)}%  (< {config.macd_pct_limit:g}%) ✅\n\n"
        "1H\n"
        f"RSI: {_fmt(row.get('rsi_1h'), 2)}  (< {config.rsi_limit:g}) ✅\n\n"
        f"Fuente: {row.get('source') or 'N/D'}\n"
        f"Última vela 1H: {row.get('bar_1h') or 'N/D'}"
    )


def send_telegram_message(bot_token: str, chat_id: str, text: str, timeout: int = 15):
    token = str(bot_token or "").strip()
    chat = str(chat_id or "").strip()
    if not token or not chat:
        return False

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text},
        timeout=timeout,
    )
    response.raise_for_status()
    return True


# ============================================================================
# HELPERS DE BARRAS
# ============================================================================

def _alpaca_rows_to_frame(rows):
    if not rows:
        return _empty_frame()
    df = pd.DataFrame(rows)
    if "t" not in df.columns or "c" not in df.columns:
        return _empty_frame()

    out = pd.DataFrame({
        "timestamp": pd.to_datetime(df["t"], utc=True, errors="coerce"),
        "close": pd.to_numeric(df["c"], errors="coerce"),
        "volume": pd.to_numeric(df.get("v", 0), errors="coerce").fillna(0.0),
    })
    return (
        out.dropna(subset=["timestamp", "close"])
        .drop_duplicates(subset=["timestamp"], keep="last")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def _empty_frame():
    return pd.DataFrame(columns=["timestamp", "close", "volume"])


def _closed_bars_only_usa(df: pd.DataFrame, timeframe: str, effective_end: datetime):
    if df is None or df.empty:
        return df

    effective_end = _ensure_utc(effective_end)
    local_end = effective_end.astimezone(NEW_YORK)
    ts_local = df["timestamp"].dt.tz_convert(NEW_YORK)

    if timeframe == "1Day":
        market_closed = local_end.time() >= dt_time(16, 0)
        mask = (
            (ts_local.dt.date < local_end.date())
            | ((ts_local.dt.date == local_end.date()) & market_closed)
        )
        return df.loc[mask].reset_index(drop=True)

    if timeframe in {"1Hour", "4Hour"}:
        hours = 1 if timeframe == "1Hour" else 4
        nominal_close = df["timestamp"] + pd.to_timedelta(hours, unit="h")
        earlier_day = ts_local.dt.date < local_end.date()
        same_day_market_closed = (
            (ts_local.dt.date == local_end.date())
            & (local_end.time() >= dt_time(16, 0))
            & (ts_local.dt.time < dt_time(16, 0))
        )
        mask = earlier_day | (nominal_close <= effective_end) | same_day_market_closed
        return df.loc[mask].reset_index(drop=True)

    return df


def _closed_bars_only_arg(df: pd.DataFrame, timeframe: str, effective_end: datetime):
    """Conservador: usa únicamente velas que sabemos cerradas."""
    if df is None or df.empty:
        return df

    effective_end = _ensure_utc(effective_end)
    local_end = effective_end.astimezone(BUENOS_AIRES)
    ts_local = df["timestamp"].dt.tz_convert(BUENOS_AIRES)

    if timeframe == "1Day":
        # El día actual se acepta sólo al final de la rueda; 18:30 da margen.
        day_closed = local_end.time() >= dt_time(18, 30)
        mask = (
            (ts_local.dt.date < local_end.date())
            | ((ts_local.dt.date == local_end.date()) & day_closed)
        )
        return df.loc[mask].reset_index(drop=True)

    if timeframe == "1Hour":
        nominal_close = df["timestamp"] + pd.to_timedelta(1, unit="h")
        return df.loc[nominal_close <= effective_end].reset_index(drop=True)

    return df


def _resample_arg_session_4h(hourly: pd.DataFrame, effective_end: datetime):
    """
    Construye 4H desde 1H por sesión, no por bloques UTC.

    Agrupa las primeras cuatro velas horarias del día y luego el bloque restante.
    El bloque corto final de una rueda anterior cuenta como vela cerrada; el del
    día actual sólo se incluye cuando la sesión ya terminó (18:30 ART).
    """
    if hourly is None or hourly.empty:
        return _empty_frame()

    work = hourly.copy().sort_values("timestamp").reset_index(drop=True)
    local_ts = work["timestamp"].dt.tz_convert(BUENOS_AIRES)
    work["session_date"] = local_ts.dt.date
    work["session_index"] = work.groupby("session_date").cumcount()
    work["block"] = work["session_index"] // 4

    effective_local = _ensure_utc(effective_end).astimezone(BUENOS_AIRES)
    rows = []

    for (session_date, block), group in work.groupby(["session_date", "block"], sort=True):
        group = group.sort_values("timestamp")
        count = len(group)
        is_today = session_date == effective_local.date()
        session_closed = (not is_today) or effective_local.time() >= dt_time(18, 30)

        # Bloque de cuatro horas completo o bloque corto de una sesión ya cerrada.
        if count < 4 and not session_closed:
            continue

        rows.append({
            "timestamp": group["timestamp"].iloc[0],
            "close": float(group["close"].iloc[-1]),
            "volume": float(pd.to_numeric(group.get("volume", 0), errors="coerce").fillna(0).sum()),
        })

    if not rows:
        return _empty_frame()
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def _empty_snapshot(df, quality):
    return {
        "rsi": None,
        "macd": None,
        "macd_signal": None,
        "macd_histogram": None,
        "macd_pct": None,
        "price": None,
        "bar_time": None if df is None or len(df) == 0 else _iso_or_none(df["timestamp"].iloc[-1]),
        "bars": 0 if df is None else int(len(df)),
        "quality": quality,
    }


def _clean_symbols(symbols):
    return list(dict.fromkeys(
        str(s).strip().upper()
        for s in symbols
        if str(s).strip()
    ))


def _rfc3339(dt: datetime) -> str:
    return _ensure_utc(dt).isoformat().replace("+00:00", "Z")


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_or_none(value):
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:
        return None


def _lt(value, threshold):
    try:
        value = float(value)
        if not math.isfinite(value):
            return None
        return bool(value < float(threshold))
    except Exception:
        return None


def _fmt(value, decimals=2):
    try:
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "N/D"


def _finite_or_zero(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else 0.0
    except Exception:
        return 0.0

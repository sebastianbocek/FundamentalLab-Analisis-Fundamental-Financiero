from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
import math
from typing import Callable, Iterable

import requests


ProgressFn = Callable[[int, int, str], None] | None


def _valid_price(v):
    try:
        v = float(v)
        return math.isfinite(v) and v > 0
    except (TypeError, ValueError):
        return False


def yahoo_symbol(symbol: str) -> str:
    """
    Convierte ticker interno al formato Yahoo.
    Ej.: BRK.B -> BRK-B. Mantiene sufijos internacionales como .BA.
    """
    symbol = str(symbol).strip().upper()
    if symbol.endswith(".BA"):
        return symbol
    return symbol.replace(".", "-")


def _chart_price(yahoo_ticker: str, timeout: float = 3.0):
    """
    Precio mediante Yahoo v8 chart.
    No usa crumb/cookie y evita el costo de crear yf.Ticker por cada símbolo.
    Prueba query1 y luego query2 sólo si hace falta.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
    params = {"range": "5d", "interval": "1d"}

    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{yahoo_ticker}"
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=(1.5, timeout),
            )
            if r.status_code != 200:
                # No insistir indefinidamente.
                continue

            result = (r.json().get("chart", {}).get("result") or [])
            if not result:
                continue

            payload = result[0]
            meta = payload.get("meta", {}) or {}

            for key in (
                "regularMarketPrice",
                "previousClose",
                "chartPreviousClose",
            ):
                value = meta.get(key)
                if _valid_price(value):
                    return float(value)

            quotes = payload.get("indicators", {}).get("quote") or []
            if quotes:
                closes = quotes[0].get("close") or []
                for value in reversed(closes):
                    if _valid_price(value):
                        return float(value)
        except Exception:
            continue

    return None


def yahoo_chart_prices_concurrent(
    symbols: Iterable[str],
    max_workers: int = 24,
    timeout: float = 3.0,
    progress: ProgressFn = None,
) -> dict[str, float]:
    """
    Consulta precios en paralelo. Nunca hace un loop de red bloqueante ticker por ticker.
    Cada request tiene timeout estricto.
    """
    originals = list(
        dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip())
    )
    if not originals:
        return {}

    mapping = {s: yahoo_symbol(s) for s in originals}
    out: dict[str, float] = {}

    workers = max(1, min(int(max_workers), 32, len(originals)))
    done = 0

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yf-chart") as ex:
        futures = {
            ex.submit(_chart_price, mapping[orig], timeout): orig
            for orig in originals
        }

        for fut in as_completed(futures):
            orig = futures[fut]
            try:
                price = fut.result()
            except Exception:
                price = None

            if _valid_price(price):
                out[orig] = float(price)

            done += 1
            if progress and (done == len(originals) or done % 20 == 0):
                progress(
                    done,
                    len(originals),
                    f"Yahoo chart: {done}/{len(originals)} · precios encontrados: {len(out)}",
                )

    return out


def _last_close_from_download(data, yahoo_ticker: str):
    if data is None or getattr(data, "empty", True):
        return None

    try:
        cols = data.columns

        # MultiIndex: normalmente ('Close', 'AAPL')
        if getattr(cols, "nlevels", 1) > 1:
            candidates = [
                ("Close", yahoo_ticker),
                (yahoo_ticker, "Close"),
            ]
            for key in candidates:
                if key in cols:
                    s = data[key].dropna()
                    if len(s) and _valid_price(s.iloc[-1]):
                        return float(s.iloc[-1])

            # Fallback sólo cuando hay una única columna Close inequívoca.
            close_cols = [c for c in cols if "Close" in c]
            if len(close_cols) == 1:
                s = data[close_cols[0]].dropna()
                if len(s) and _valid_price(s.iloc[-1]):
                    return float(s.iloc[-1])

        elif "Close" in cols:
            s = data["Close"].dropna()
            if len(s) and _valid_price(s.iloc[-1]):
                return float(s.iloc[-1])

    except Exception:
        return None

    return None


def yfinance_batch_fallback(
    symbols: Iterable[str],
    batch_size: int = 120,
    timeout: float = 5.0,
    progress: ProgressFn = None,
) -> dict[str, float]:
    """
    Fallback POR LOTES únicamente para precios que Yahoo chart no resolvió.

    Importante:
    - no usa yf.Ticker individual;
    - timeout explícito;
    - número acotado de lotes.
    """
    try:
        import yfinance as yf
    except Exception:
        return {}

    originals = list(
        dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip())
    )
    if not originals:
        return {}

    mapping = {s: yahoo_symbol(s) for s in originals}
    out: dict[str, float] = {}
    total_batches = math.ceil(len(originals) / batch_size)

    for batch_index, start in enumerate(range(0, len(originals), batch_size), start=1):
        batch_orig = originals[start:start + batch_size]
        batch_yahoo = [mapping[s] for s in batch_orig]

        try:
            data = yf.download(
                tickers=batch_yahoo,
                period="5d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=12,
                group_by="column",
                timeout=timeout,
            )
        except Exception:
            data = None

        for orig, yt in zip(batch_orig, batch_yahoo):
            price = _last_close_from_download(data, yt)
            if _valid_price(price):
                out[orig] = float(price)

        if progress:
            progress(
                batch_index,
                total_batches,
                f"Fallback yfinance: lote {batch_index}/{total_batches} · "
                f"precios encontrados: {len(out)}",
            )

    return out


def yahoo_prices_fast(
    symbols: Iterable[str],
    progress: ProgressFn = None,
) -> dict[str, float]:
    """
    Estrategia V2.4:

      1) Yahoo chart directo en paralelo (rápido, sin crumb).
      2) yfinance.download por lotes SÓLO para faltantes.
      3) No existe fallback individual serial.

    Por diseño, esta función termina en tiempo acotado aunque Yahoo falle.
    """
    originals = list(
        dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip())
    )
    if not originals:
        return {}

    if progress:
        progress(0, len(originals), f"Buscando precio para {len(originals)} candidatos...")

    out = yahoo_chart_prices_concurrent(
        originals,
        max_workers=24,
        timeout=3.0,
        progress=progress,
    )

    missing = [s for s in originals if s not in out]

    # Sólo una segunda capa por lotes. Nunca miles de yf.Ticker individuales.
    if missing:
        fallback = yfinance_batch_fallback(
            missing,
            batch_size=120,
            timeout=5.0,
            progress=progress,
        )
        out.update(fallback)

    return out


# Compatibilidad con imports viejos.
yahoo_batch_prices = yahoo_prices_fast


def argentina_yahoo_prices_fast(
    symbols: Iterable[str],
    progress: ProgressFn = None,
) -> dict[str, float]:
    """
    Fallback para acciones argentinas.

    Entrada:
        ["LEDE", "MIRG", "ALUA"]

    Consulta Yahoo como:
        LEDE.BA, MIRG.BA, ALUA.BA

    Salida:
        {"LEDE": 1234.5, "MIRG": 22000.0, ...}
    """
    originals = list(
        dict.fromkeys(
            str(s).strip().upper()
            for s in symbols
            if str(s).strip()
        )
    )
    if not originals:
        return {}

    yahoo_symbols = [
        s if s.endswith(".BA") else f"{s}.BA"
        for s in originals
    ]

    fetched = yahoo_prices_fast(yahoo_symbols, progress=progress)

    out = {}
    for orig, ys in zip(originals, yahoo_symbols):
        value = fetched.get(ys)
        if _valid_price(value):
            out[orig] = float(value)

    return out


def _chart_quote_details(
    yahoo_ticker: str,
    timeout: float = 4.0,
    range_: str = "3mo",
):
    """
    Quote detallado desde Yahoo chart.

    A diferencia de _chart_price(), devuelve la fecha REAL del último punto
    de mercado. Esto permite distinguir:
      - precio actual;
      - símbolo existente pero con última operación de hace años.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
    params = {
        "range": range_,
        "interval": "1d",
        "includePrePost": "false",
        "events": "div,splits",
    }

    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{yahoo_ticker}"
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=(1.5, timeout),
            )
            if r.status_code != 200:
                continue

            result = (r.json().get("chart", {}).get("result") or [])
            if not result:
                continue

            payload = result[0]
            meta = payload.get("meta", {}) or {}
            timestamps = payload.get("timestamp") or []
            quotes = payload.get("indicators", {}).get("quote") or []

            last_price = None
            last_timestamp = None

            if quotes:
                closes = quotes[0].get("close") or []
                for ts, close in reversed(list(zip(timestamps, closes))):
                    if _valid_price(close):
                        last_price = float(close)
                        try:
                            last_timestamp = int(ts)
                        except Exception:
                            last_timestamp = None
                        break

            # Si no hubo close usable, aceptar regularMarketPrice sólo si
            # también existe regularMarketTime.
            if last_price is None:
                rmp = meta.get("regularMarketPrice")
                rmt = meta.get("regularMarketTime")
                if _valid_price(rmp) and rmt:
                    last_price = float(rmp)
                    try:
                        last_timestamp = int(rmt)
                    except Exception:
                        last_timestamp = None

            if last_price is None or last_timestamp is None:
                continue

            dt = datetime.fromtimestamp(last_timestamp, tz=timezone.utc)
            age_days = max(
                0,
                (datetime.now(timezone.utc).date() - dt.date()).days,
            )

            return {
                "price": last_price,
                "timestamp": last_timestamp,
                "date": dt.date().isoformat(),
                "age_days": age_days,
                "currency": meta.get("currency"),
                "exchange": (
                    meta.get("exchangeName")
                    or meta.get("fullExchangeName")
                    or meta.get("exchange")
                ),
                "instrument_type": meta.get("instrumentType"),
                "symbol": meta.get("symbol") or yahoo_ticker,
            }
        except Exception:
            continue

    return None


def argentina_yahoo_fresh_quotes(
    symbols: Iterable[str],
    max_age_days: int = 14,
    max_workers: int = 16,
    progress: ProgressFn = None,
) -> dict[str, dict]:
    """
    Fallback seguro para precio argentino.

    Sólo acepta Yahoo cuando:
      - consulta EXACTAMENTE TICKER.BA;
      - existe un último close con fecha real;
      - ese close tiene <= max_age_days;
      - currency es ARS o Yahoo no informa currency;
      - el instrumento es EQUITY o Yahoo no informa tipo.

    Esto bloquea clases antiguas como ROSE5 cuando Yahoo conserva un precio
    residual pero el último trade es viejo.
    """
    originals = list(dict.fromkeys(
        str(s).strip().upper()
        for s in symbols
        if str(s).strip()
    ))
    if not originals:
        return {}

    workers = max(1, min(int(max_workers), 24, len(originals)))
    out = {}
    done = 0

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="arg-yahoo") as ex:
        futures = {
            ex.submit(
                _chart_quote_details,
                s if s.endswith(".BA") else f"{s}.BA",
                4.0,
                "3mo",
            ): s
            for s in originals
        }

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                quote = future.result()
            except Exception:
                quote = None

            if quote:
                currency = str(quote.get("currency") or "").upper()
                instrument = str(quote.get("instrument_type") or "").upper()
                fresh = int(quote.get("age_days", 10**9)) <= int(max_age_days)
                currency_ok = currency in ("", "ARS")
                instrument_ok = instrument in ("", "EQUITY")

                if fresh and currency_ok and instrument_ok and _valid_price(quote.get("price")):
                    quote["source"] = "Yahoo .BA fresh"
                    out[symbol] = quote

            done += 1
            if progress and (done == len(originals) or done % 20 == 0):
                progress(
                    done,
                    len(originals),
                    f"Yahoo .BA fresco: {done}/{len(originals)} · aceptados: {len(out)}",
                )

    return out


@lru_cache(maxsize=16)
def yahoo_fx_to_ars(currency: str) -> float | None:
    """
    Convierte una unidad de currency a ARS usando Yahoo.

    Ejemplos:
      USD -> ticker ARS=X  (USD/ARS)
      EUR -> ticker EURARS=X

    ARS retorna 1.
    """
    currency = str(currency or "").upper().strip()
    if not currency:
        return None
    if currency == "ARS":
        return 1.0

    ticker = "ARS=X" if currency == "USD" else f"{currency}ARS=X"
    quote = _chart_quote_details(ticker, timeout=4.0, range_="5d")
    if not quote:
        return None

    price = quote.get("price")
    age_days = quote.get("age_days")
    if not _valid_price(price):
        return None
    if age_days is None or int(age_days) > 7:
        return None

    return float(price)


def usa_yahoo_fresh_quotes(
    symbols: Iterable[str],
    max_age_days: int = 10,
    max_workers: int = 24,
    progress: ProgressFn = None,
) -> dict[str, dict]:
    """
    Quote detallado para acciones USA.

    Devuelve por ticker:
      price
      date
      age_days
      currency
      exchange
      instrument_type
      source

    Usa el timestamp real del último close, por lo que la UI puede mostrar
    'Último trade' en vez de dejar la columna vacía.
    """
    originals = list(dict.fromkeys(
        str(s).strip().upper()
        for s in symbols
        if str(s).strip()
    ))
    if not originals:
        return {}

    workers = max(
        1,
        min(int(max_workers), 32, len(originals)),
    )

    out = {}
    done = 0

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="usa-yahoo",
    ) as executor:
        futures = {
            executor.submit(
                _chart_quote_details,
                yahoo_symbol(symbol),
                4.0,
                "1mo",
            ): symbol
            for symbol in originals
        }

        for future in as_completed(futures):
            symbol = futures[future]

            try:
                quote = future.result()
            except Exception:
                quote = None

            if quote:
                age_days = quote.get("age_days")
                instrument = str(
                    quote.get("instrument_type")
                    or ""
                ).upper()

                fresh = (
                    age_days is not None
                    and int(age_days) <= int(max_age_days)
                )
                instrument_ok = instrument in (
                    "",
                    "EQUITY",
                )

                if (
                    fresh
                    and instrument_ok
                    and _valid_price(
                        quote.get("price")
                    )
                ):
                    quote["source"] = "Yahoo chart fresh"
                    out[symbol] = quote

            done += 1
            if progress and (
                done == len(originals)
                or done % 25 == 0
            ):
                progress(
                    done,
                    len(originals),
                    (
                        f"Yahoo USA: {done}/{len(originals)} · "
                        f"quotes frescos: {len(out)}"
                    ),
                )

    return out

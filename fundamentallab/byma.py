from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from .database import upsert_company
from .models import CompanyContext
from .prices import argentina_yahoo_fresh_quotes, yahoo_fx_to_ars


BASE = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"


class BymaClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 FundamentalLab/2.8",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def post(self, endpoint, payload):
        url = f"{BASE}/{endpoint}"
        try:
            r = self.s.post(url, json=payload, timeout=45)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError:
            warnings.warn("BYMA SSL chain: retry verify=False")
            r = self.s.post(
                url,
                json=payload,
                timeout=45,
                verify=False,
            )
            r.raise_for_status()
            return r.json()

    def equities(self):
        """
        Universo de acciones locales.

        Filtra:
          - mercado BYMA;
          - securityType CS;
          - moneda ARS;
        y prioriza settlement 24hs.
        """
        frames = []
        payload = {
            "T0": True,
            "T1": True,
            "T2": True,
            "page_size": 5000,
            "excludeZeroPxAndQty": False,
        }

        for endpoint in ("leading-equity", "general-equity"):
            raw = self.post(endpoint, payload)
            data = raw.get("data", []) if isinstance(raw, dict) else raw

            if data:
                df = pd.DataFrame(data)
                df["panel_source"] = endpoint
                frames.append(df)

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)

        if "market" in df.columns:
            filtered = df[df["market"].astype(str).eq("BYMA")]
            if not filtered.empty:
                df = filtered

        if "securityType" in df.columns:
            filtered = df[df["securityType"].astype(str).eq("CS")]
            if not filtered.empty:
                df = filtered

        if "denominationCcy" in df.columns:
            filtered = df[df["denominationCcy"].astype(str).eq("ARS")]
            if not filtered.empty:
                df = filtered

        if "settlementType" in df.columns:
            # 2 = 24hs, 1 = CI, 3 = 48hs.
            priority = {"2": 0, "1": 1, "3": 2}
            df["_settlement_priority"] = (
                df["settlementType"]
                .astype(str)
                .map(priority)
                .fillna(9)
            )
            df = df.sort_values(
                ["symbol", "_settlement_priority"]
            )

        return df.drop_duplicates("symbol", keep="first").copy()

    def history(self, symbol, lookback_days=180):
        """
        Serie oficial diaria de BYMA.

        BYMA espera:
            symbol = "GGAL 24HS"
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(lookback_days))

        url = f"{BASE}/chart/historical-series/history"
        params = {
            "symbol": f"{symbol} 24HS",
            "resolution": "D",
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
        }

        try:
            r = self.s.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError:
            warnings.warn(
                "BYMA SSL chain: history retry verify=False"
            )
            r = self.s.get(
                url,
                params=params,
                timeout=15,
                verify=False,
            )
            r.raise_for_status()
            return r.json()

    def latest_recent_close(
        self,
        symbol,
        lookback_days=180,
        max_age_days=30,
    ):
        """
        Último cierre oficial BYMA.

        Si el último dato tiene más de max_age_days, se considera stale
        y NO se devuelve como precio vigente.
        """
        try:
            raw = self.history(
                symbol,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            return {
                "price": None,
                "date": None,
                "age_days": None,
                "status": "error",
                "error": str(exc),
            }

        if (
            not isinstance(raw, dict)
            or raw.get("s") != "ok"
        ):
            return {
                "price": None,
                "date": None,
                "age_days": None,
                "status": "no_data",
            }

        timestamps = raw.get("t") or []
        closes = raw.get("c") or []

        valid = []
        for stamp, close in zip(timestamps, closes):
            try:
                stamp = int(stamp)
                close = float(close)
                if close > 0:
                    valid.append((stamp, close))
            except Exception:
                continue

        if not valid:
            return {
                "price": None,
                "date": None,
                "age_days": None,
                "status": "no_data",
            }

        stamp, close = valid[-1]
        dt = datetime.fromtimestamp(
            stamp,
            tz=timezone.utc,
        )
        age_days = max(
            0,
            (
                datetime.now(timezone.utc).date()
                - dt.date()
            ).days,
        )

        if age_days <= int(max_age_days):
            return {
                "price": close,
                "date": dt.date().isoformat(),
                "age_days": age_days,
                "status": "recent",
            }

        return {
            "price": None,
            "historical_price": close,
            "date": dt.date().isoformat(),
            "age_days": age_days,
            "status": "stale",
        }

    def refresh_universe(self, conn):
        df = self.equities()

        for _, row in df.iterrows():
            symbol = str(
                row.get("symbol", "")
            ).upper().strip()

            if not symbol:
                continue

            name = str(
                row.get("securityDesc")
                or row.get("description")
                or symbol
            ).strip()

            upsert_company(
                conn,
                "ARG",
                symbol,
                name,
                None,
                [],
                f"BYMA {row.get('panel_source', 'equities')}",
            )

        conn.commit()
        return df


def find_row(frame, names):
    if frame is None or frame.empty:
        return None

    exact = {
        str(index).lower().strip(): index
        for index in frame.index
    }

    for name in names:
        key = name.lower().strip()
        if key in exact:
            return frame.loc[exact[key]]

    for index in frame.index:
        low = str(index).lower()
        if any(name.lower() in low for name in names):
            return frame.loc[index]

    return None


def latest4(row):
    if row is None:
        return [], []

    series = pd.to_numeric(
        row,
        errors="coerce",
    ).dropna()

    if series.empty:
        return [], []

    series = series.iloc[:4]

    pairs = list(zip(
        [str(col)[:10] for col in series.index],
        [float(value) for value in series.values],
    ))
    pairs.reverse()

    return (
        [p[0] for p in pairs],
        [p[1] for p in pairs],
    )


def align(periods, period_source, values):
    mapping = dict(zip(period_source, values))
    return [mapping.get(period) for period in periods]


def _positive_number(value):
    try:
        if value is None or pd.isna(value):
            return None

        value = float(value)
        return value if value > 0 else None
    except Exception:
        return None


def byma_price_from_row(row):
    """
    Mejor precio disponible en el snapshot BYMA.

    Este valor todavía NO implica que sea reciente.
    La frescura se valida por separado.
    """
    fields = [
        ("trade", "BYMA trade"),
        ("closingPrice", "BYMA closingPrice"),
        ("settlementPrice", "BYMA settlementPrice"),
        (
            "previousClosingPrice",
            "BYMA previousClosingPrice",
        ),
        (
            "previousSettlementPrice",
            "BYMA previousSettlementPrice",
        ),
        ("vwap", "BYMA vwap"),
        ("openingPrice", "BYMA openingPrice"),
    ]

    for field, source in fields:
        try:
            raw = row.get(field)
        except Exception:
            raw = None

        price = _positive_number(raw)
        if price is not None:
            return price, source

    return None, None


def byma_snapshot_has_activity(row):
    """
    Señal de que el snapshot pertenece a una rueda con actividad.

    Una clase antigua puede conservar un precio residual durante años,
    pero normalmente no tendrá órdenes/volumen/monto en la rueda actual.
    """
    fields = (
        "numberOfOrders",
        "volume",
        "tradeVolume",
        "volumeAmount",
    )

    for field in fields:
        try:
            value = row.get(field)
        except Exception:
            value = None

        try:
            if (
                value is not None
                and not pd.isna(value)
                and float(value) > 0
            ):
                return True
        except Exception:
            pass

    return False


def validate_argentina_snapshot_prices(
    client,
    rows,
    max_workers=5,
    lookback_days=180,
    max_age_days=30,
    progress=None,
):
    """
    Fuente de verdad de precios ARG:

      snapshot BYMA con actividad
          ↓ si no
      histórico BYMA 24HS
          ↓
      si último cierre <= 30 días -> válido
      si no -> N/D / stale

    Yahoo nunca participa del precio argentino.
    """
    result = {}
    needs_history = []

    for row in rows:
        symbol = str(
            row.get("symbol", "")
        ).upper().strip()

        if not symbol:
            continue

        snapshot_price, snapshot_source = (
            byma_price_from_row(row)
        )
        active = byma_snapshot_has_activity(row)

        if active and snapshot_price is not None:
            result[symbol] = {
                "price": snapshot_price,
                "source": snapshot_source,
                "status": "valid_snapshot",
                "last_trade_date": None,
                "age_days": 0,
            }
        else:
            needs_history.append(
                (
                    symbol,
                    snapshot_price,
                    snapshot_source,
                )
            )

    total = len(needs_history)
    workers = max(
        1,
        min(
            int(max_workers),
            5,
            total or 1,
        ),
    )

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = {
            executor.submit(
                client.latest_recent_close,
                symbol,
                lookback_days,
                max_age_days,
            ): (
                symbol,
                snapshot_price,
                snapshot_source,
            )
            for (
                symbol,
                snapshot_price,
                snapshot_source,
            ) in needs_history
        }

        done = 0

        for future in as_completed(futures):
            (
                symbol,
                snapshot_price,
                snapshot_source,
            ) = futures[future]

            try:
                hist = future.result()
            except Exception as exc:
                hist = {
                    "price": None,
                    "date": None,
                    "age_days": None,
                    "status": "error",
                    "error": str(exc),
                }

            status = hist.get("status")

            if (
                status == "recent"
                and hist.get("price")
            ):
                result[symbol] = {
                    "price": float(
                        hist["price"]
                    ),
                    "source": (
                        f"BYMA history "
                        f"{hist.get('date')}"
                    ),
                    "status": "valid_history",
                    "last_trade_date": hist.get(
                        "date"
                    ),
                    "age_days": hist.get(
                        "age_days"
                    ),
                }

            elif status == "stale":
                result[symbol] = {
                    "price": None,
                    "source": (
                        "BYMA stale quote blocked"
                    ),
                    "status": "stale",
                    "last_trade_date": hist.get(
                        "date"
                    ),
                    "age_days": hist.get(
                        "age_days"
                    ),
                    "stale_price": hist.get(
                        "historical_price",
                        snapshot_price,
                    ),
                }

            else:
                result[symbol] = {
                    "price": None,
                    "source": (
                        "BYMA no recent quote"
                    ),
                    "status": "no_recent_quote",
                    "last_trade_date": hist.get(
                        "date"
                    ),
                    "age_days": hist.get(
                        "age_days"
                    ),
                    "snapshot_price_rejected": (
                        snapshot_price
                    ),
                }

            done += 1

            if progress and (
                done == total
                or done % 10 == 0
            ):
                progress(
                    done,
                    total,
                    (
                        "Validando cotizaciones "
                        f"BYMA: {done}/{total}"
                    ),
                )

    return result


def _empty_arg_context(
    symbol,
    price,
    name,
    reason,
    yahoo_symbol=None,
):
    return CompanyContext(
        "ARG",
        symbol,
        name or symbol,
        ["Q-3", "Q-2", "Q-1", "Q0"],
        [None, None, None, None],
        [None, None, None, None],
        [None, None, None, None],
        [None, None, None, None],
        [None, None, None, None],
        [None, None, None, None],
        [None, None, None, None],
        price,
        "ARS",
        {
            "source": "BYMA universe/price",
            "yahoo_symbol": yahoo_symbol,
            "fundamentals_status": "unavailable",
            "fundamentals_reason": reason,
        },
    )


def yahoo_arg_context(symbol, price, name):
    """
    Estados financieros de Yahoo para una especie argentina.

    El PRECIO no se obtiene acá: llega ya validado desde BYMA/Yahoo-fresh.
    Esta función sólo normaliza fundamentals y metadata para el DCF.
    """
    import yfinance as yf

    yahoo_symbol = (
        symbol
        if symbol.endswith(".BA")
        else symbol + ".BA"
    )

    info = {}
    annual_cashflow = pd.DataFrame()

    try:
        ticker = yf.Ticker(yahoo_symbol)
        income = ticker.quarterly_income_stmt
        cashflow = ticker.quarterly_cashflow
        balance = ticker.quarterly_balance_sheet

        try:
            annual_cashflow = ticker.cashflow
            if annual_cashflow is None:
                annual_cashflow = pd.DataFrame()
        except Exception:
            annual_cashflow = pd.DataFrame()

        try:
            info = ticker.get_info() or {}
        except Exception:
            info = {}

    except Exception as exc:
        return _empty_arg_context(
            symbol,
            price,
            name,
            (
                "Yahoo no pudo descargar "
                f"estados trimestrales: {exc}"
            ),
            yahoo_symbol,
        )

    if income is None or income.empty:
        return _empty_arg_context(
            symbol,
            price,
            name,
            (
                "Yahoo no devolvió estado "
                "de resultados trimestral."
            ),
            yahoo_symbol,
        )

    if cashflow is None:
        cashflow = pd.DataFrame()

    if balance is None:
        balance = pd.DataFrame()

    rows = {
        "revenue": find_row(
            income,
            [
                "Total Revenue",
                "Operating Revenue",
            ],
        ),
        "net_income": find_row(
            income,
            [
                "Net Income",
                "Net Income Common Stockholders",
            ],
        ),
        "cfo": find_row(
            cashflow,
            [
                "Operating Cash Flow",
                "Total Cash From Operating Activities",
            ],
        ),
        "fcf": find_row(
            cashflow,
            ["Free Cash Flow"],
        ),
        "capex": find_row(
            cashflow,
            [
                "Capital Expenditure",
                "Capital Expenditures",
            ],
        ),
        "buybacks": find_row(
            cashflow,
            [
                "Repurchase Of Capital Stock",
                "Repurchase Of Stock",
            ],
        ),
        "shares": find_row(
            balance,
            [
                "Ordinary Shares Number",
                "Share Issued",
            ],
        ),
    }

    bundle = {
        key: latest4(value)
        for key, value in rows.items()
    }

    period_candidates = [
        bundle["revenue"][0],
        bundle["net_income"][0],
        bundle["cfo"][0],
    ]

    periods = (
        max(period_candidates, key=len)
        if period_candidates
        else []
    )

    if not periods:
        return _empty_arg_context(
            symbol,
            price,
            name,
            (
                "Yahoo devolvió estados, "
                "pero no pudo extraerse "
                "ningún quarter."
            ),
            yahoo_symbol,
        )

    periods = periods[-4:]

    if len(periods) < 4:
        missing = 4 - len(periods)
        periods = (
            [
                f"Q-ND-{i+1}"
                for i in range(missing)
            ]
            + periods
        )

    def align4(item):
        source_periods, values = item
        return align(
            periods,
            source_periods,
            values,
        )[-4:]

    revenue = align4(bundle["revenue"])
    net_income = align4(bundle["net_income"])
    cfo = align4(bundle["cfo"])
    capex_raw = align4(bundle["capex"])
    fcf_direct = align4(bundle["fcf"])
    buybacks_raw = align4(bundle["buybacks"])
    shares = align4(bundle["shares"])

    capex = [
        abs(value)
        if value is not None
        else None
        for value in capex_raw
    ]

    fcf = []
    for direct_fcf, operating_cf, capital_expenditure in zip(
        fcf_direct,
        cfo,
        capex,
    ):
        if direct_fcf is not None:
            fcf.append(float(direct_fcf))
        elif (
            operating_cf is not None
            and capital_expenditure is not None
        ):
            fcf.append(
                float(operating_cf)
                - float(capital_expenditure)
            )
        else:
            fcf.append(None)

    buybacks = [
        abs(value)
        if value is not None
        else None
        for value in buybacks_raw
    ]

    # ---------------------------------------------------------------
    # Share count fallback.
    # ---------------------------------------------------------------
    if any(value is None for value in shares):
        try:
            share_history = ticker.get_shares_full(
                start=None,
                end=None,
            )
            if (
                share_history is not None
                and len(share_history.dropna())
            ):
                latest_share_count = float(
                    share_history.dropna().iloc[-1]
                )
                shares = [
                    latest_share_count
                    if value is None
                    else value
                    for value in shares
                ]
        except Exception:
            pass

    complete_4q = (
        sum(
            value is not None
            for value in revenue
        ) >= 4
        and sum(
            value is not None
            for value in net_income
        ) >= 4
    )

    status = (
        "ok"
        if complete_4q
        else "partial"
    )

    # ---------------------------------------------------------------
    # Currency / shares / annual FCF metadata.
    # ---------------------------------------------------------------
    financial_currency = str(
        info.get("financialCurrency")
        or ""
    ).upper().strip() or None

    market_currency = str(
        info.get("currency")
        or ""
    ).upper().strip() or "ARS"

    shares_outstanding = None
    for key in (
        "sharesOutstanding",
        "impliedSharesOutstanding",
    ):
        try:
            candidate = float(info.get(key))
            if candidate > 0:
                shares_outstanding = candidate
                break
        except Exception:
            pass

    market_cap = None
    try:
        candidate = float(info.get("marketCap"))
        if candidate > 0:
            market_cap = candidate
    except Exception:
        pass

    if (
        shares_outstanding is None
        and market_cap is not None
        and price is not None
        and price > 0
        and market_currency == "ARS"
    ):
        derived = market_cap / price
        if derived > 0:
            shares_outstanding = derived

    latest_fy_fcf = None

    if (
        annual_cashflow is not None
        and not annual_cashflow.empty
    ):
        annual_fcf_row = find_row(
            annual_cashflow,
            ["Free Cash Flow"],
        )
        annual_cfo_row = find_row(
            annual_cashflow,
            [
                "Operating Cash Flow",
                "Total Cash From Operating Activities",
            ],
        )
        annual_capex_row = find_row(
            annual_cashflow,
            [
                "Capital Expenditure",
                "Capital Expenditures",
            ],
        )

        if annual_fcf_row is not None:
            vals = pd.to_numeric(
                annual_fcf_row,
                errors="coerce",
            ).dropna()
            if len(vals):
                try:
                    latest_fy_fcf = float(
                        vals.iloc[0]
                    )
                except Exception:
                    pass

        if (
            latest_fy_fcf is None
            and annual_cfo_row is not None
            and annual_capex_row is not None
        ):
            cfo_vals = pd.to_numeric(
                annual_cfo_row,
                errors="coerce",
            ).dropna()
            capex_vals = pd.to_numeric(
                annual_capex_row,
                errors="coerce",
            ).dropna()

            if len(cfo_vals) and len(capex_vals):
                try:
                    latest_fy_fcf = (
                        float(cfo_vals.iloc[0])
                        - abs(float(capex_vals.iloc[0]))
                    )
                except Exception:
                    latest_fy_fcf = None

    ttm_fcf = None
    if (
        len(fcf) >= 4
        and all(
            value is not None
            for value in fcf[-4:]
        )
    ):
        try:
            ttm_fcf = sum(
                float(value)
                for value in fcf[-4:]
            )
        except Exception:
            ttm_fcf = None

    fx_to_ars = None
    if financial_currency:
        try:
            fx_to_ars = yahoo_fx_to_ars(
                financial_currency
            )
        except Exception:
            fx_to_ars = None

    return CompanyContext(
        "ARG",
        symbol,
        name or symbol,
        periods[-4:],
        revenue[-4:],
        net_income[-4:],
        cfo[-4:],
        capex[-4:],
        fcf[-4:],
        buybacks[-4:],
        shares[-4:],
        price,
        "ARS",
        {
            "source": (
                "validated local price + "
                "yfinance financials"
            ),
            "yahoo_symbol": yahoo_symbol,
            "fundamentals_status": status,
            "financial_currency": financial_currency,
            "market_currency": market_currency,
            "dcf_currency_multiplier": fx_to_ars,
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "latest_fy_fcf": latest_fy_fcf,
            "latest_fy_fcf_source": (
                "Yahoo annual cashflow"
                if latest_fy_fcf is not None
                else None
            ),
            "ttm_fcf": ttm_fcf,
            "ttm_fcf_source": (
                "Yahoo quarterly FCF 4Q"
                if ttm_fcf is not None
                else None
            ),
            "coverage_counts": {
                "revenue": sum(
                    value is not None
                    for value in revenue
                ),
                "net_income": sum(
                    value is not None
                    for value in net_income
                ),
                "fcf": sum(
                    value is not None
                    for value in fcf
                ),
                "shares": sum(
                    value is not None
                    for value in shares
                ),
            },
        },
    )

def build_argentina_contexts(
    conn,
    client,
    max_workers=5,
    limit=None,
    progress=None,
):
    """
    Crea un contexto por especie BYMA.

    Precio:
      1) BYMA snapshot con actividad.
      2) BYMA histórico 24HS reciente.
      3) Yahoo TICKER.BA sólo con último close <= 14 días.

    Fundamentals:
      Yahoo se usa únicamente para estados financieros y metadata.
    """
    universe = client.refresh_universe(conn)

    if limit:
        scan_universe = universe.head(int(limit)).copy()
    else:
        scan_universe = universe.copy()

    raw_rows = [
        row
        for _, row in scan_universe.iterrows()
    ]

    # ---------------------------------------------------------------
    # 1) BYMA price validation.
    # ---------------------------------------------------------------
    price_validation = validate_argentina_snapshot_prices(
        client,
        raw_rows,
        max_workers=max_workers,
        lookback_days=180,
        max_age_days=30,
        progress=progress,
    )

    # ---------------------------------------------------------------
    # 2) Yahoo fresh fallback only for BYMA gaps.
    # ---------------------------------------------------------------
    missing_byma = [
        str(row.get("symbol", "")).upper().strip()
        for row in raw_rows
        if str(row.get("symbol", "")).strip()
        and not price_validation.get(
            str(row.get("symbol", "")).upper().strip(),
            {},
        ).get("price")
    ]

    def yahoo_price_progress(done, total, message):
        if progress:
            progress(done, total, message)

    yahoo_fresh = argentina_yahoo_fresh_quotes(
        missing_byma,
        max_age_days=14,
        max_workers=min(max_workers * 3, 18),
        progress=yahoo_price_progress,
    )

    for symbol in missing_byma:
        quote = yahoo_fresh.get(symbol)
        if not quote:
            continue

        current = price_validation.get(symbol, {})

        # Si BYMA tiene una referencia relativamente reciente, usarla como
        # control de coherencia para evitar mezclar clases/tickers.
        reference = (
            current.get("stale_price")
            or current.get("snapshot_price_rejected")
        )
        ref_age = current.get("age_days")

        accept = True
        try:
            if (
                reference is not None
                and float(reference) > 0
                and ref_age is not None
                and int(ref_age) <= 180
            ):
                ratio = float(quote["price"]) / float(reference)
                if ratio < 0.10 or ratio > 10.0:
                    accept = False
        except Exception:
            pass

        if accept:
            price_validation[symbol] = {
                "price": float(quote["price"]),
                "source": f"Yahoo .BA fresh {quote.get('date')}",
                "status": "valid_yahoo_fresh",
                "last_trade_date": quote.get("date"),
                "age_days": quote.get("age_days"),
                "yahoo_currency": quote.get("currency"),
                "yahoo_exchange": quote.get("exchange"),
            }

    # ---------------------------------------------------------------
    # 3) Build work list.
    # ---------------------------------------------------------------
    work = []

    for row in raw_rows:
        symbol = str(
            row.get("symbol", "")
        ).upper().strip()

        if not symbol:
            continue

        name = str(
            row.get("securityDesc")
            or row.get("description")
            or symbol
        )

        validation = price_validation.get(
            symbol,
            {},
        )

        work.append((
            symbol,
            validation.get("price"),
            name,
            validation.get("source"),
            validation.get("status"),
            validation.get("last_trade_date"),
            validation.get("age_days"),
            validation.get("stale_price"),
        ))

    # ---------------------------------------------------------------
    # 4) Fetch financial statements concurrently.
    # ---------------------------------------------------------------
    contexts = []
    total = len(work)
    done = 0

    workers = max(
        1,
        min(
            int(max_workers),
            10,
        ),
    )

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = {
            executor.submit(
                yahoo_arg_context,
                item[0],
                item[1],
                item[2],
            ): item
            for item in work
        }

        for future in as_completed(futures):
            (
                symbol,
                price,
                name,
                price_source,
                price_status,
                last_trade_date,
                age_days,
                stale_price,
            ) = futures[future]

            try:
                ctx = future.result()
            except Exception as exc:
                ctx = _empty_arg_context(
                    symbol,
                    price,
                    name,
                    (
                        "Error inesperado "
                        "normalizando fundamentals: "
                        f"{exc}"
                    ),
                    symbol + ".BA",
                )

            if ctx is None:
                ctx = _empty_arg_context(
                    symbol,
                    price,
                    name,
                    "Proveedor no devolvió contexto.",
                    symbol + ".BA",
                )

            ctx.metadata["byma_price_source"] = price_source
            ctx.metadata["byma_price_status"] = price_status
            ctx.metadata["byma_last_trade_date"] = last_trade_date
            ctx.metadata["byma_price_age_days"] = age_days

            if price is not None:
                ctx.metadata["byma_price"] = price

            if stale_price is not None:
                ctx.metadata[
                    "byma_stale_price_rejected"
                ] = stale_price

            contexts.append(ctx)
            done += 1

            if progress and (
                done == total
                or done % 20 == 0
            ):
                progress(
                    done,
                    total,
                    (
                        "Argentina fundamentals: "
                        f"{done}/{total}"
                    ),
                )

    contexts.sort(
        key=lambda ctx: ctx.symbol
    )

    return contexts, universe

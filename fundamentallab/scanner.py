from __future__ import annotations

from typing import Callable

from .criteria import CRITERIA, MIN_CRITERIA_TO_SURFACE, intrinsic_value_per_share
from .database import store_context, store_result, latest_known_prices
from .engine import evaluate_company
from .prices import usa_yahoo_fresh_quotes, yfinance_batch_fallback
from .sec_companyfacts import enrich_contexts_selectively


ProgressFn = Callable[[str, int, int, str], None] | None

# Límite del segundo pase SEC CompanyFacts. Se priorizan primero las compañías
# que ya tienen más señales positivas y luego las que pueden llegar al filtro.
MAX_COMPANYFACTS_ENRICH = 600


def _criterion_without_price(ctx, key):
    for item in CRITERIA:
        if item.key == key:
            return item.evaluator(ctx)
    return None


def pre_matches(ctx):
    """Cantidad de criterios fundamentales que ya pasan SIN usar precio."""
    return sum(
        int(c.evaluator(ctx).passed)
        for c in CRITERIA
        if c.key != "below_intrinsic"
    )


def _needs_companyfacts(ctx) -> bool:
    """
    CompanyFacts se usa sólo cuando puede mejorar cobertura:
    - FCF no evaluable; o
    - DCF/intrínseco todavía no calculable.

    La selección final se ordena por prioridad para no pedir miles de JSON.
    """
    fcf_result = _criterion_without_price(ctx, "fcf_growth")
    intrinsic, _ = intrinsic_value_per_share(ctx)
    fcf_missing = fcf_result is None or not getattr(fcf_result, "evaluable", True)
    return fcf_missing or intrinsic is None


def _companyfacts_priority(ctx) -> float:
    """
    Prioridad de enriquecimiento:
    1) compañías que ya tienen 2+ señales;
    2) compañías con 1 señal y huecos de FCF/DCF;
    3) compañías con 0 señales pero donde FCF+valuación todavía podrían crear 2/4.
    """
    pm = pre_matches(ctx)
    fcf_result = _criterion_without_price(ctx, "fcf_growth")
    intrinsic, _ = intrinsic_value_per_share(ctx)

    score = pm * 100.0
    if fcf_result is not None and not getattr(fcf_result, "evaluable", True):
        score += 30.0
    if intrinsic is None:
        score += 20.0

    # Revenue + NI completos hacen más probable poder reconstruir fiscal quarters.
    score += sum(v is not None for v in (ctx.revenue or [])) * 1.0
    score += sum(v is not None for v in (ctx.net_income or [])) * 1.0
    return score


def _needs_price(ctx, matches_without_price: int) -> tuple[bool, str]:
    """
    Decide si vale la pena consultar precio.

    - >= mínimo: ya es candidata y queremos mostrar precio + upside.
    - mínimo-1: sólo precio si el DCF ya es calculable; subvaluación podría elevarla.
    - por debajo: precio no cambia su clasificación actual.
    """
    if matches_without_price >= MIN_CRITERIA_TO_SURFACE:
        return True, "ya cumple el mínimo fundamental"

    if matches_without_price == MIN_CRITERIA_TO_SURFACE - 1:
        intrinsic, _ = intrinsic_value_per_share(ctx)
        if intrinsic is not None:
            return True, "puede llegar al mínimo si está subvaluada"

    return False, "el precio no puede cambiar su clasificación actual"


def scan_usa(
    conn,
    run_id,
    contexts,
    sec_client=None,
    progress: ProgressFn = None,
):
    """
    USA scanner with complete metadata.

    1) SEC Frames bulk universe.
    2) Selective CompanyFacts enrichment.
    3) Selective price plan.
    4) Yahoo chart fresh quote with date/source.
    5) Batch fallback, then SQLite cache.
    6) Final DCF evaluation with global status metadata.
    """
    total_contexts = len(contexts)

    # ---------------------------------------------------------------
    # PHASE 1 — selective SEC CompanyFacts enrichment.
    # ---------------------------------------------------------------
    if sec_client is not None:
        if progress:
            candidates_count = sum(
                1
                for ctx in contexts
                if _needs_companyfacts(ctx)
            )
            selected = min(
                candidates_count,
                MAX_COMPANYFACTS_ENRICH,
            )

            progress(
                "coverage_plan",
                0,
                max(1, selected),
                (
                    f"Cobertura SEC: {candidates_count} con huecos FCF/DCF; "
                    f"se enriquecerán hasta {selected} por prioridad."
                ),
            )

        def cf_progress(done, total, message):
            if progress:
                progress(
                    "coverage",
                    done,
                    total,
                    message,
                )

        contexts, stats = enrich_contexts_selectively(
            contexts,
            sec_client,
            predicate=_needs_companyfacts,
            priority=_companyfacts_priority,
            max_companies=MAX_COMPANYFACTS_ENRICH,
            max_workers=4,
            progress=cf_progress,
        )

        if progress:
            progress(
                "coverage_done",
                stats.get("selected", 0),
                max(1, stats.get("selected", 0)),
                (
                    f"Cobertura lista: "
                    f"{stats.get('enriched', 0)} CompanyFacts procesadas · "
                    f"{stats.get('fcf_complete', 0)} con FCF 4Q reconstruido."
                ),
            )

    # ---------------------------------------------------------------
    # PHASE 2 — persist fundamentals and plan prices.
    # ---------------------------------------------------------------
    price_symbols = []
    strong = 0
    upgradeable = 0

    for idx, ctx in enumerate(
        contexts,
        start=1,
    ):
        source = (
            (ctx.metadata or {}).get(
                "source",
                "SEC XBRL Frames",
            )
        )

        # Normalize USA metadata even after CompanyFacts enrichment.
        ctx.metadata = dict(
            ctx.metadata or {}
        )
        ctx.metadata.setdefault(
            "financial_currency",
            "USD",
        )
        ctx.metadata.setdefault(
            "market_currency",
            "USD",
        )
        ctx.metadata.setdefault(
            "dcf_currency_multiplier",
            1.0,
        )

        store_context(
            conn,
            ctx,
            source,
        )

        pm = pre_matches(ctx)
        needed, _ = _needs_price(
            ctx,
            pm,
        )

        if needed:
            price_symbols.append(
                ctx.symbol
            )

            if (
                pm
                >= MIN_CRITERIA_TO_SURFACE
            ):
                strong += 1
            else:
                upgradeable += 1

        if (
            progress
            and (
                idx == total_contexts
                or idx % 500 == 0
            )
        ):
            progress(
                "evaluate",
                idx,
                total_contexts,
                (
                    f"Pre-evaluación: {idx}/{total_contexts} · "
                    f"requieren precio: {len(price_symbols)}"
                ),
            )

    conn.commit()

    price_symbols = list(
        dict.fromkeys(price_symbols)
    )

    if progress:
        progress(
            "price_plan",
            0,
            max(1, len(price_symbols)),
            (
                f"Plan de precios: {len(price_symbols)} tickers "
                f"({strong} ya prioritarios + "
                f"{upgradeable} potencialmente elevables)."
            ),
        )

    # ---------------------------------------------------------------
    # PHASE 3 — prices with metadata.
    # ---------------------------------------------------------------
    cached = latest_known_prices(
        conn,
        "USA",
        price_symbols,
    )

    def price_progress(
        done,
        total,
        message,
    ):
        if progress:
            progress(
                "prices",
                done,
                total,
                message,
            )

    quote_details = (
        usa_yahoo_fresh_quotes(
            price_symbols,
            max_age_days=10,
            max_workers=24,
            progress=price_progress,
        )
    )

    prices = {
        symbol: float(
            quote["price"]
        )
        for symbol, quote
        in quote_details.items()
        if quote.get("price")
        is not None
    }

    missing_after_chart = [
        symbol
        for symbol in price_symbols
        if symbol not in prices
    ]

    batch_fallback = {}

    if missing_after_chart:
        batch_fallback = (
            yfinance_batch_fallback(
                missing_after_chart,
                batch_size=120,
                timeout=5.0,
                progress=price_progress,
            )
        )

        prices.update(
            batch_fallback
        )

    if progress:
        progress(
            "prices_done",
            len(price_symbols),
            max(1, len(price_symbols)),
            (
                f"Yahoo fresh: {len(quote_details)} · "
                f"batch fallback: {len(batch_fallback)} · "
                f"cache disponible: {len(cached)}"
            ),
        )

    # ---------------------------------------------------------------
    # PHASE 4 — final evaluation.
    # ---------------------------------------------------------------
    out = []
    intrinsic_count = 0
    fcf_evaluable_count = 0

    for idx, ctx in enumerate(
        contexts,
        start=1,
    ):
        fresh_quote = (
            quote_details.get(
                ctx.symbol
            )
        )

        if fresh_quote is not None:
            ctx.price = float(
                fresh_quote["price"]
            )

            ctx.metadata[
                "price_source_final"
            ] = fresh_quote.get(
                "source",
                "Yahoo chart fresh",
            )

            ctx.metadata[
                "price_last_date"
            ] = fresh_quote.get(
                "date"
            )

            ctx.metadata[
                "price_age_days"
            ] = fresh_quote.get(
                "age_days"
            )

            ctx.metadata[
                "price_exchange"
            ] = fresh_quote.get(
                "exchange"
            )

            ctx.metadata[
                "price_currency"
            ] = (
                fresh_quote.get(
                    "currency"
                )
                or "USD"
            )

        elif (
            ctx.symbol
            in batch_fallback
        ):
            ctx.price = float(
                batch_fallback[
                    ctx.symbol
                ]
            )

            ctx.metadata[
                "price_source_final"
            ] = (
                "yfinance batch fallback"
            )

            ctx.metadata[
                "price_last_date"
            ] = None

            ctx.metadata[
                "price_currency"
            ] = "USD"

        else:
            ctx.price = cached.get(
                ctx.symbol
            )

            if (
                ctx.price
                is not None
            ):
                ctx.metadata[
                    "price_source_final"
                ] = "SQLite price cache"

                ctx.metadata[
                    "price_last_date"
                ] = None

                ctx.metadata[
                    "price_currency"
                ] = "USD"

            else:
                ctx.metadata[
                    "price_source_final"
                ] = "N/D"

                ctx.metadata[
                    "price_last_date"
                ] = None

        ctx.currency = "USD"

        ctx.metadata.setdefault(
            "financial_currency",
            "USD",
        )

        ctx.metadata.setdefault(
            "market_currency",
            "USD",
        )

        ctx.metadata.setdefault(
            "dcf_currency_multiplier",
            1.0,
        )

        result = evaluate_company(
            ctx
        )

        if (
            result.intrinsic_value
            is not None
        ):
            intrinsic_count += 1

        fcf_result = next(
            (
                criterion
                for criterion
                in result.criteria
                if criterion.key
                == "fcf_growth"
            ),
            None,
        )

        if (
            fcf_result is not None
            and getattr(
                fcf_result,
                "evaluable",
                True,
            )
        ):
            fcf_evaluable_count += 1

        store_result(
            conn,
            run_id,
            result,
        )

        out.append(
            result
        )

        if (
            progress
            and (
                idx
                == total_contexts
                or idx % 500 == 0
            )
        ):
            progress(
                "store",
                idx,
                total_contexts,
                (
                    f"Guardando: {idx}/{total_contexts} · "
                    f"FCF evaluable: {fcf_evaluable_count} · "
                    f"DCF calculado: {intrinsic_count}"
                ),
            )

    conn.commit()
    return out

def scan_argentina(conn, run_id, contexts, progress: ProgressFn = None):
    """
    V2.8 — Argentina price safety.

    BYMA es la fuente primaria; Yahoo .BA sólo puede actuar como fallback con timestamp fresco.

    El precio ya viene validado desde build_argentina_contexts():
      - snapshot con actividad de rueda; o
      - último cierre reciente del histórico BYMA.

    Si BYMA no tiene cotización reciente:
      - price = None
      - Upside = N/D
      - Subvaluada = N/D

    Yahoo .BA sólo se acepta si el último close real tiene <=14 días. Una clase antigua/ilíquida con quote stale queda N/D.
    """
    total = len(contexts)
    out = []

    valid_prices = 0
    stale_prices = 0
    missing_prices = 0
    intrinsic_count = 0
    upside_count = 0

    for idx, ctx in enumerate(contexts, start=1):
        store_context(conn, ctx, "BYMA + yfinance fundamentals")

        status = (ctx.metadata or {}).get("byma_price_status")

        if status == "stale":
            # Defensa extra: aunque algún caller haya dejado un precio viejo,
            # una cotización marcada stale jamás puede usarse.
            ctx.price = None
            stale_prices += 1
            ctx.metadata["price_source_final"] = "N/D — stale BYMA quote"
            ctx.metadata["price_validation"] = "not_current"

        elif ctx.price is not None and float(ctx.price or 0) > 0:
            valid_prices += 1
            ctx.metadata["price_source_final"] = (
                ctx.metadata.get("byma_price_source") or "BYMA"
            )
            ctx.metadata["price_validation"] = "validated_byma"

        else:
            # No permitir que otra fuente reintroduzca una cotización obsoleta.
            ctx.price = None
            missing_prices += 1
            ctx.metadata["price_source_final"] = "N/D — no recent BYMA quote"
            ctx.metadata["price_validation"] = "not_current"

        ctx.currency = "ARS"

        result = evaluate_company(ctx)

        if result.intrinsic_value is not None:
            intrinsic_count += 1
        if result.upside_to_intrinsic is not None:
            upside_count += 1

        store_result(conn, run_id, result)
        out.append(result)

        if progress and (idx == total or idx % 50 == 0):
            progress(
                "arg_results",
                idx,
                total,
                f"Argentina {idx}/{total} · "
                f"precios BYMA válidos {valid_prices} · "
                f"stale bloqueados {stale_prices} · "
                f"sin cotización reciente {missing_prices} · "
                f"intrínseco {intrinsic_count} · upside {upside_count}",
            )

    conn.commit()
    return out

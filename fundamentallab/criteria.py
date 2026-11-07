# ==========================================================================
# FUNDAMENTALLAB V2.2 - ESTRATEGIA FUNDAMENTAL "OPPORTUNITY SCANNER"
# ==========================================================================
#
# OBJETIVO:
#   Detectar oportunidades reales sin exigir secuencias trimestrales perfectas.
#
# FILOSOFÍA:
#   1) FCF: mejora entre el quarter más reciente y el de 3 quarters atrás.
#   2) Buybacks: prioriza reducción REAL de diluted shares; el cash gastado
#      en recompras se usa como evidencia secundaria.
#   3) Margen neto: busca expansión entre extremos y/o mejora del promedio
#      reciente, no crecimiento perfecto Q1<Q2<Q3<Q4.
#   4) Valor intrínseco: DCF de FCF normalizado, con crecimiento suavizado
#      y límites conservadores.
#
# IMPORTANTE:
#   - El scanner sigue mostrando primero compañías con >= 2 criterios.
#   - Este archivo puede modificarse sin tocar scanner.py ni database.py.
# ==========================================================================

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Callable
import math

from .models import CompanyContext, CriterionResult


# ==========================================================================
# CONFIGURACIÓN GENERAL
# ==========================================================================

QUARTERS_TO_ANALYZE = 4

# Mostrar como candidata prioritaria si cumple 2 de 4.
MIN_CRITERIA_TO_SURFACE = 2


# ==========================================================================
# CONFIGURACIÓN FCF
# ==========================================================================

# Criterio principal pedido:
# FCF último quarter > FCF de 3 quarters atrás.
#
# 0.00 = simplemente tiene que ser mayor.
# 0.05 = exigiría +5%.
FCF_MIN_ENDPOINT_GROWTH = 0.00


# ==========================================================================
# CONFIGURACIÓN RECOMPRAS
# ==========================================================================

# Una reducción >= 0.25% del diluted share count en ~3 quarters
# ya se considera evidencia positiva de recompra neta.
BUYBACK_MIN_SHARE_REDUCTION = 0.0025

# Si no hay reducción suficiente de acciones, también puede pasar cuando:
# - hay cash de recompras reportado;
# - el gasto reciente en recompras es mayor al inicial;
# - y la dilución neta no supera este límite.
BUYBACK_MAX_ALLOWED_DILUTION = 0.005

# Número mínimo de quarters con dato explícito de buybacks para usar
# el cash gastado como evidencia alternativa.
BUYBACK_MIN_REPORTED_QUARTERS = 2


# ==========================================================================
# CONFIGURACIÓN MARGEN NETO
# ==========================================================================

# Expansión mínima entre Q1 y Q4: 50 basis points.
NET_MARGIN_MIN_ENDPOINT_EXPANSION = 0.005

# Alternativa más robusta:
# promedio(Q3,Q4) > promedio(Q1,Q2) por al menos 25 bps.
NET_MARGIN_MIN_AVERAGE_EXPANSION = 0.0025


# ==========================================================================
# CONFIGURACIÓN DCF / VALOR INTRÍNSECO
# ==========================================================================

DCF_PROJECTION_YEARS = 5

# 10% es deliberadamente exigente para un filtro general.
DCF_DISCOUNT_RATE = 0.10

# Crecimiento perpetuo conservador.
DCF_TERMINAL_GROWTH = 0.025

# El crecimiento explícito se estima a partir de FCF + revenue,
# pero se "encoge" hacia este valor para evitar extrapolar ruido.
DCF_DEFAULT_GROWTH = 0.04

# Límites del crecimiento anual usado en DCF.
DCF_GROWTH_FLOOR = 0.00
DCF_GROWTH_CAP = 0.12

# Cuánto confiar en el crecimiento observado.
# 0.50 = mitad crecimiento observado + mitad default.
DCF_OBSERVED_GROWTH_WEIGHT = 0.50

# Normalización de FCF:
# mezcla TTM real con 4 * mediana trimestral.
# Reduce el efecto de un quarter extraordinariamente alto/bajo.
DCF_RAW_TTM_WEIGHT = 0.60
DCF_MEDIAN_TTM_WEIGHT = 0.40

# 0.00 = basta con precio < valor intrínseco.
# 0.15 = exigir 15% de margen de seguridad.
INTRINSIC_MARGIN_OF_SAFETY = 0.00

# Argentina: controles de calidad del DCF.
# Si financials están en USD/EUR y el precio está en ARS, convertir a ARS.
DCF_ARG_ENABLE_CURRENCY_CONVERSION = True

# Rechazar sólo extremos casi seguramente incompatibles de unidad/moneda.
# Ej.: intrínseco 430 vs precio 0.01 => 43.000x -> inválido.
DCF_ARG_MIN_REASONABLE_PRICE_RATIO = 0.01
DCF_ARG_MAX_REASONABLE_PRICE_RATIO = 20.0


# ==========================================================================
# REGISTRO DE CRITERIOS
# ==========================================================================

@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    evaluator: Callable[[CompanyContext], CriterionResult]


# ==========================================================================
# HELPERS
# ==========================================================================

def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _complete(values: list[float | None], n: int = QUARTERS_TO_ANALYZE):
    """
    Devuelve exactamente los últimos N valores cuando todos existen.
    """
    if values is None or len(values) < n:
        return None

    vals = values[-n:]
    if any(not _finite(v) for v in vals):
        return None

    return [float(v) for v in vals]


def _partial(values: list[float | None], n: int = QUARTERS_TO_ANALYZE):
    """
    Mantiene None para criterios que pueden trabajar con cobertura parcial.
    """
    if values is None:
        return [None] * n

    vals = list(values[-n:])
    if len(vals) < n:
        vals = [None] * (n - len(vals)) + vals

    return [float(v) if _finite(v) else None for v in vals]


def _safe_change(first: float, latest: float):
    """
    Cambio porcentual robusto.

    Cuando first <= 0, el porcentaje tradicional puede ser engañoso,
    por eso devuelve None y se evalúa el cambio absoluto.
    """
    if first <= 0:
        return None
    return latest / first - 1.0


def _average(values):
    vals = [float(v) for v in values if _finite(v)]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _annualize_three_quarter_change(first: float, latest: float):
    """
    Q1 -> Q4 abarca aproximadamente 3 quarters.
    Annualiza sólo cuando ambos valores son positivos.
    """
    if first <= 0 or latest <= 0:
        return None

    ratio = latest / first
    if ratio <= 0:
        return None

    try:
        return ratio ** (4.0 / 3.0) - 1.0
    except Exception:
        return None


def _clamp(value: float, floor: float, cap: float):
    return max(floor, min(cap, value))


# ==========================================================================
# CRITERIO 1: FLUJO DE CAJA LIBRE
# ==========================================================================

def criterion_fcf_growth(ctx: CompanyContext):
    """
    VERDE si el FCF más reciente es mayor al FCF de 3 quarters atrás.

    NO exige:
        Q1 < Q2 < Q3 < Q4

    Sí acepta:
        100 -> 70 -> 90 -> 110

    También acepta turnarounds:
        -20 -> -10 -> 5 -> 15

    porque el objetivo del item es detectar MEJORA del FCF.
    """
    vals = _complete(ctx.fcf)

    if vals is None:
        return CriterionResult(
            "fcf_growth",
            "Flujo de caja libre en crecimiento",
            False,
            ctx.fcf[-4:] if ctx.fcf else [],
            "No hay 4 quarters completos de FCF.",
            evaluable=False,
        )

    first = vals[0]
    latest = vals[-1]

    if first > 0:
        change = latest / first - 1.0
        passed = change > FCF_MIN_ENDPOINT_GROWTH
    else:
        # Con base <= 0, no usamos porcentaje.
        change = None
        passed = latest > first

    ttm_fcf = sum(vals)
    avg_old = _average(vals[:2])
    avg_new = _average(vals[-2:])

    if change is None:
        change_text = "N/D por base <= 0"
    else:
        change_text = f"{change:.1%}"

    reason = (
        f"FCF Q-3={first:,.2f}; FCF actual={latest:,.2f}; "
        f"cambio={change_text}; FCF TTM={ttm_fcf:,.2f}; "
        f"promedio primeros 2Q={avg_old:,.2f}; "
        f"promedio últimos 2Q={avg_new:,.2f}. "
        f"{'Cumple: el FCF reciente mejoró.' if passed else 'No cumple: el FCF reciente no supera al de 3 quarters atrás.'}"
    )

    return CriterionResult(
        "fcf_growth",
        "Flujo de caja libre en crecimiento",
        passed,
        {
            "quarters": vals,
            "first": first,
            "latest": latest,
            "change": change,
            "ttm_fcf": ttm_fcf,
            "avg_old": avg_old,
            "avg_new": avg_new,
        },
        reason,
    )


# ==========================================================================
# CRITERIO 2: RECOMPRAS / REDUCCIÓN REAL DE ACCIONES
# ==========================================================================

def criterion_buyback_growth(ctx: CompanyContext):
    """
    Un scanner serio no debería exigir que el CASH gastado en buybacks crezca
    todos los quarters.

    Lo que beneficia al accionista es que la recompra sea EFECTIVA:
    que disminuya el diluted share count.

    PRIORIDAD:
    A) VERDE si diluted shares Q4 < diluted shares Q1 en al menos 0.25%.

    FALLBACK:
    B) Si shares no bajó lo suficiente, puede pasar si:
       - existen >= 2 quarters con buybacks reportados;
       - el cash reciente de recompras es mayor que el cash inicial disponible;
       - y no existe dilución material (>0.5%).

    Esto evita premiar compañías que gastan fortunas recomprando acciones
    mientras stock compensation diluye al accionista.
    """
    shares = _complete(ctx.diluted_shares)
    buybacks = _partial(ctx.buybacks)

    share_change = None
    share_reduction = None

    if shares is not None and shares[0] > 0:
        share_change = shares[-1] / shares[0] - 1.0
        share_reduction = -share_change

        if share_reduction >= BUYBACK_MIN_SHARE_REDUCTION:
            return CriterionResult(
                "buyback_growth",
                "Recompras / reducción de acciones",
                True,
                {
                    "diluted_shares": shares,
                    "share_change": share_change,
                    "buybacks": buybacks,
                },
                (
                    f"Diluted shares bajaron {share_reduction:.2%} "
                    f"({shares[0]:,.2f} -> {shares[-1]:,.2f}). "
                    "Cumple por reducción neta real de acciones."
                ),
            )

    reported = [(i, v) for i, v in enumerate(buybacks) if v is not None]

    if len(reported) >= BUYBACK_MIN_REPORTED_QUARTERS:
        first_idx, first_buyback = reported[0]
        last_idx, last_buyback = reported[-1]

        total_buybacks = sum(max(0.0, v) for _, v in reported)

        buyback_improved = (
            total_buybacks > 0
            and last_buyback > first_buyback
        )

        dilution_ok = (
            share_change is None
            or share_change <= BUYBACK_MAX_ALLOWED_DILUTION
        )

        passed = buyback_improved and dilution_ok

        return CriterionResult(
            "buyback_growth",
            "Recompras / reducción de acciones",
            passed,
            {
                "diluted_shares": shares,
                "share_change": share_change,
                "buybacks": buybacks,
                "reported_quarters": len(reported),
                "total_buybacks": total_buybacks,
                "first_reported_buyback": first_buyback,
                "last_reported_buyback": last_buyback,
            },
            (
                f"Buybacks reportados en {len(reported)}/4 quarters; "
                f"cash inicial reportado={first_buyback:,.2f}; "
                f"cash reciente={last_buyback:,.2f}; "
                f"total={total_buybacks:,.2f}; "
                f"cambio diluted shares="
                f"{'N/D' if share_change is None else f'{share_change:.2%}'}. "
                f"{'Cumple.' if passed else 'No cumple: no hay reducción neta suficiente ni aceleración de recompras sin dilución material.'}"
            ),
        )

    # Si hay 4 quarters de shares, la ausencia de reducción es un rojo real.
    # Si tampoco tenemos shares completos ni suficiente cash de buybacks, es N/D.
    evaluable = shares is not None or len(reported) >= BUYBACK_MIN_REPORTED_QUARTERS
    return CriterionResult(
        "buyback_growth",
        "Recompras / reducción de acciones",
        False,
        {
            "diluted_shares": shares,
            "share_change": share_change,
            "buybacks": buybacks,
            "reported_quarters": len(reported),
        },
        (
            "No hay reducción neta suficiente de acciones."
            if evaluable
            else "Datos insuficientes para evaluar recompra neta o reducción de acciones."
        ),
        evaluable=evaluable,
    )


# ==========================================================================
# CRITERIO 3: EXPANSIÓN DEL MARGEN NETO
# ==========================================================================

def net_margins(ctx: CompanyContext):
    """
    Net margin = Net Income / Revenue.
    """
    margins = []

    revenues = _partial(ctx.revenue)
    net_income = _partial(ctx.net_income)

    for ni, rev in zip(net_income, revenues):
        if ni is None or rev is None or rev == 0:
            margins.append(None)
        else:
            margins.append(float(ni) / float(rev))

    return margins


def criterion_net_margin_growth(ctx: CompanyContext):
    """
    NO exige que el margen suba en los 3 saltos trimestrales.

    Pasa si existe una mejora económicamente relevante por cualquiera de estas:

    A) Margen Q4 > margen Q1 por >= 50 bps.

    O

    B) Promedio(Q3,Q4) > promedio(Q1,Q2) por >= 25 bps.

    La segunda condición reduce ruido por seasonality o un quarter excepcional.
    """
    margins = net_margins(ctx)

    if any(v is None for v in margins):
        return CriterionResult(
            "net_margin_growth",
            "Margen neto en expansión",
            False,
            margins,
            "No hay revenue + net income completos para los últimos 4 quarters.",
            evaluable=False,
        )

    first = margins[0]
    latest = margins[-1]

    avg_old = (margins[0] + margins[1]) / 2.0
    avg_new = (margins[2] + margins[3]) / 2.0

    endpoint_expansion = latest - first
    average_expansion = avg_new - avg_old

    endpoint_pass = endpoint_expansion >= NET_MARGIN_MIN_ENDPOINT_EXPANSION
    average_pass = average_expansion >= NET_MARGIN_MIN_AVERAGE_EXPANSION

    passed = endpoint_pass or average_pass

    reason = (
        f"Margen Q-3={first:.2%}; margen actual={latest:.2%}; "
        f"expansión extremos={endpoint_expansion * 10000:.0f} bps. "
        f"Promedio primeros 2Q={avg_old:.2%}; "
        f"promedio últimos 2Q={avg_new:.2%}; "
        f"expansión promedios={average_expansion * 10000:.0f} bps. "
        f"{'Cumple: existe expansión material del margen.' if passed else 'No cumple: la mejora del margen no alcanza el mínimo requerido.'}"
    )

    return CriterionResult(
        "net_margin_growth",
        "Margen neto en expansión",
        passed,
        {
            "margins": margins,
            "endpoint_expansion": endpoint_expansion,
            "average_expansion": average_expansion,
            "avg_old": avg_old,
            "avg_new": avg_new,
        },
        reason,
    )


# ==========================================================================
# CRECIMIENTO SOSTENIBLE PARA DCF
# ==========================================================================

def _estimate_sustainable_growth(ctx: CompanyContext, fcf: list[float]):
    """
    Evita anualizar ciegamente la mediana QoQ.

    Usa señales de:
      - FCF Q1 -> Q4
      - Revenue Q1 -> Q4

    Las anualiza sólo cuando los extremos son positivos, toma la mediana,
    la limita y luego mezcla 50/50 con un crecimiento base conservador.

    Si no hay señal utilizable -> DCF_DEFAULT_GROWTH.
    """
    observed = []

    fcf_growth = _annualize_three_quarter_change(fcf[0], fcf[-1])
    if fcf_growth is not None and math.isfinite(fcf_growth):
        observed.append(fcf_growth)

    revenues = _complete(ctx.revenue)
    if revenues is not None:
        revenue_growth = _annualize_three_quarter_change(
            revenues[0],
            revenues[-1],
        )
        if revenue_growth is not None and math.isfinite(revenue_growth):
            observed.append(revenue_growth)

    if not observed:
        return DCF_DEFAULT_GROWTH, {
            "observed_growth": None,
            "used_growth": DCF_DEFAULT_GROWTH,
            "signals": [],
        }

    observed_growth = median(observed)

    # Primero limitamos el dato extremo.
    observed_growth = _clamp(
        observed_growth,
        DCF_GROWTH_FLOOR,
        DCF_GROWTH_CAP,
    )

    # Luego lo acercamos al crecimiento base.
    growth = (
        DCF_OBSERVED_GROWTH_WEIGHT * observed_growth
        + (1.0 - DCF_OBSERVED_GROWTH_WEIGHT) * DCF_DEFAULT_GROWTH
    )

    growth = _clamp(
        growth,
        DCF_GROWTH_FLOOR,
        DCF_GROWTH_CAP,
    )

    return growth, {
        "observed_growth": observed_growth,
        "used_growth": growth,
        "signals": observed,
    }


# ==========================================================================
# NORMALIZACIÓN DEL FCF
# ==========================================================================

def _normalized_ttm_fcf(fcf: list[float]):
    """
    TTM puro puede quedar distorsionado por un quarter extraordinario.

    Mezclamos:
        60% * suma real 4Q
        40% * (mediana quarter * 4)

    Si la mediana es <= 0, usamos el TTM real para no fabricar FCF positivo.
    """
    raw_ttm = sum(fcf)
    median_q = median(fcf)
    median_ttm = median_q * 4.0

    if median_q <= 0:
        normalized = raw_ttm
    else:
        normalized = (
            DCF_RAW_TTM_WEIGHT * raw_ttm
            + DCF_MEDIAN_TTM_WEIGHT * median_ttm
        )

    return normalized, {
        "raw_ttm": raw_ttm,
        "median_q": median_q,
        "median_ttm": median_ttm,
        "normalized_ttm": normalized,
    }


# ==========================================================================
# VALOR INTRÍNSECO
# ==========================================================================

def _valid_positive(values):
    """Valores finitos y positivos, conservando orden temporal."""
    if not values:
        return []
    return [float(v) for v in values[-QUARTERS_TO_ANALYZE:] if _finite(v) and float(v) > 0]


def intrinsic_value_per_share(ctx: CompanyContext):
    """
    DCF simplificado de flujo de caja libre por acción.

    Cobertura:
      - 4 quarters completos de FCF cuando existen;
      - fallback TTM/FY desde metadata;
      - share counts parciales o shares_outstanding;
      - Argentina: conversión de financialCurrency a ARS;
      - control de calidad contra precio para evitar mezcla de unidades.
    """
    fcf = _complete(ctx.fcf)
    dcf_fcf_source = "4 quarters de FCF"

    annual_fcf_fallback = None
    if fcf is None and getattr(ctx, "metadata", None):
        for key in ("ttm_fcf", "latest_fy_fcf"):
            candidate = ctx.metadata.get(key)
            if _finite(candidate) and float(candidate) > 0:
                annual_fcf_fallback = float(candidate)
                dcf_fcf_source = ctx.metadata.get(
                    (
                        "ttm_fcf_source"
                        if key == "ttm_fcf"
                        else "latest_fy_fcf_source"
                    ),
                    key,
                )
                break

    if fcf is None and annual_fcf_fallback is None:
        return None, (
            "No hay 4 quarters completos de FCF ni un "
            "FCF anual/TTM válido de fallback."
        )

    valid_shares = _valid_positive(
        ctx.diluted_shares
    )

    if not valid_shares:
        fallback = None
        if getattr(ctx, "metadata", None):
            for key in (
                "shares_outstanding",
                "latest_shares",
                "common_shares_outstanding",
            ):
                candidate = ctx.metadata.get(key)
                if (
                    _finite(candidate)
                    and float(candidate) > 0
                ):
                    fallback = float(candidate)
                    break

        if fallback is not None:
            valid_shares = [fallback]

    if not valid_shares:
        return None, (
            "No hay ningún share count válido "
            "para calcular FCF por acción."
        )

    if fcf is not None:
        normalized_fcf, fcf_info = (
            _normalized_ttm_fcf(fcf)
        )
        raw_fcf_for_reason = (
            fcf_info["raw_ttm"]
        )
        growth, _ = (
            _estimate_sustainable_growth(
                ctx,
                fcf,
            )
        )
    else:
        normalized_fcf = annual_fcf_fallback
        raw_fcf_for_reason = (
            annual_fcf_fallback
        )
        growth = DCF_DEFAULT_GROWTH

    if (
        normalized_fcf is None
        or normalized_fcf <= 0
    ):
        return None, (
            f"FCF utilizable no positivo "
            f"({normalized_fcf:,.2f}); "
            "DCF no aplicable con este método."
        )

    if len(valid_shares) >= 2:
        normalized_shares = (
            valid_shares[-1]
            + valid_shares[-2]
        ) / 2.0
        shares_note = (
            "promedio últimos 2 share counts "
            f"válidos ({valid_shares[-2]:,.2f}, "
            f"{valid_shares[-1]:,.2f})"
        )
    else:
        normalized_shares = valid_shares[-1]
        shares_note = (
            "último share count válido "
            f"({normalized_shares:,.2f})"
        )

    if normalized_shares <= 0:
        return None, "Share count normalizado inválido."

    fcf_per_share = (
        normalized_fcf
        / normalized_shares
    )

    r = DCF_DISCOUNT_RATE
    tg = DCF_TERMINAL_GROWTH

    if r <= tg:
        return None, (
            "La tasa de descuento debe ser "
            "mayor al crecimiento terminal."
        )

    projected = fcf_per_share
    pv_explicit = 0.0

    for year in range(
        1,
        DCF_PROJECTION_YEARS + 1,
    ):
        projected *= 1.0 + growth
        pv_explicit += (
            projected
            / ((1.0 + r) ** year)
        )

    terminal_value = (
        projected
        * (1.0 + tg)
        / (r - tg)
    )

    pv_terminal = (
        terminal_value
        / (
            (1.0 + r)
            ** DCF_PROJECTION_YEARS
        )
    )

    intrinsic = (
        pv_explicit
        + pv_terminal
    )

    # ---------------------------------------------------------------
    # Argentina: financial statements pueden venir en USD/EUR mientras
    # la acción cotiza en ARS.
    # ---------------------------------------------------------------
    currency_note = ""

    if (
        ctx.market == "ARG"
        and DCF_ARG_ENABLE_CURRENCY_CONVERSION
        and getattr(ctx, "metadata", None)
    ):
        financial_currency = str(
            ctx.metadata.get(
                "financial_currency"
            )
            or ""
        ).upper().strip()

        market_currency = str(
            ctx.metadata.get(
                "market_currency"
            )
            or ctx.currency
            or "ARS"
        ).upper().strip()

        multiplier = ctx.metadata.get(
            "dcf_currency_multiplier"
        )

        if (
            financial_currency
            and market_currency == "ARS"
            and financial_currency != "ARS"
        ):
            if (
                _finite(multiplier)
                and float(multiplier) > 0
            ):
                original_intrinsic = intrinsic
                intrinsic *= float(multiplier)
                currency_note = (
                    f" Conversión "
                    f"{financial_currency}->ARS "
                    f"x{float(multiplier):,.4f}; "
                    f"DCF antes de FX="
                    f"{original_intrinsic:,.2f}."
                )
            else:
                return None, (
                    f"DCF calculado en "
                    f"{financial_currency}, pero "
                    "no hay FX válido para "
                    "convertirlo a ARS."
                )

    # ---------------------------------------------------------------
    # Argentina: sanity check contra precio actual.
    # ---------------------------------------------------------------
    if (
        ctx.market == "ARG"
        and ctx.price is not None
        and _finite(ctx.price)
        and float(ctx.price) > 0
    ):
        ratio_to_price = (
            intrinsic
            / float(ctx.price)
        )

        if (
            ratio_to_price
            < DCF_ARG_MIN_REASONABLE_PRICE_RATIO
            or ratio_to_price
            > DCF_ARG_MAX_REASONABLE_PRICE_RATIO
        ):
            if (
                getattr(
                    ctx,
                    "metadata",
                    None,
                )
                is not None
            ):
                ctx.metadata[
                    "dcf_rejected_raw_intrinsic"
                ] = intrinsic
                ctx.metadata[
                    "dcf_rejected_ratio_to_price"
                ] = ratio_to_price
                ctx.metadata[
                    "dcf_quality_status"
                ] = "rejected_extreme_ratio"

            return None, (
                "DCF rechazado por control de "
                "calidad: intrínseco/precio="
                f"{ratio_to_price:.2f}x fuera "
                "del rango "
                f"{DCF_ARG_MIN_REASONABLE_PRICE_RATIO:.2f}x-"
                f"{DCF_ARG_MAX_REASONABLE_PRICE_RATIO:.2f}x. "
                "Probable incompatibilidad de "
                "moneda, escala o share count."
            )

    if (
        getattr(
            ctx,
            "metadata",
            None,
        )
        is not None
    ):
        ctx.metadata[
            "dcf_quality_status"
        ] = "ok"

    reason = (
        f"DCF/acción={intrinsic:,.2f}; "
        f"FCF base={raw_fcf_for_reason:,.2f}; "
        f"FCF normalizado={normalized_fcf:,.2f}; "
        f"fuente FCF={dcf_fcf_source}; "
        f"FCF/share={fcf_per_share:,.2f}; "
        f"shares={shares_note}; "
        f"growth usado={growth:.1%}; "
        f"discount={r:.1%}; "
        f"terminal={tg:.1%}."
        f"{currency_note}"
    )

    return intrinsic, reason

# ==========================================================================
# CRITERIO 4: PRECIO VS VALOR INTRÍNSECO
# ==========================================================================

def criterion_below_intrinsic(ctx: CompanyContext):
    intrinsic, why = intrinsic_value_per_share(ctx)

    if intrinsic is None:
        return CriterionResult(
            "below_intrinsic",
            "Precio por debajo del valor intrínseco",
            False,
            None,
            why,
            evaluable=False,
        )

    if ctx.price is None or not _finite(ctx.price) or float(ctx.price) <= 0:
        return CriterionResult(
            "below_intrinsic",
            "Precio por debajo del valor intrínseco",
            False,
            {"intrinsic": intrinsic},
            f"{why} Falta precio de mercado válido.",
            evaluable=False,
        )

    price = float(ctx.price)

    threshold = (
        intrinsic * (1.0 - INTRINSIC_MARGIN_OF_SAFETY)
    )

    passed = price < threshold
    upside = intrinsic / price - 1.0

    return CriterionResult(
        "below_intrinsic",
        "Precio por debajo del valor intrínseco",
        passed,
        {
            "intrinsic": intrinsic,
            "price": price,
            "threshold": threshold,
            "upside": upside,
            "margin_of_safety": INTRINSIC_MARGIN_OF_SAFETY,
        },
        (
            f"{why} "
            f"Precio={price:,.2f}; "
            f"umbral={threshold:,.2f}; "
            f"upside al intrínseco={upside:.1%}. "
            f"{'Cumple: cotiza debajo del valor intrínseco requerido.' if passed else 'No cumple: el precio no ofrece el descuento requerido.'}"
        ),
    )


# ==========================================================================
# REGISTRO DINÁMICO
# ==========================================================================

CRITERIA = [
    Criterion(
        "fcf_growth",
        "Flujo de caja libre en crecimiento",
        criterion_fcf_growth,
    ),
    Criterion(
        "buyback_growth",
        "Recompras / reducción de acciones",
        criterion_buyback_growth,
    ),
    Criterion(
        "net_margin_growth",
        "Margen neto en expansión",
        criterion_net_margin_growth,
    ),
    Criterion(
        "below_intrinsic",
        "Precio por debajo del valor intrínseco",
        criterion_below_intrinsic,
    ),
]

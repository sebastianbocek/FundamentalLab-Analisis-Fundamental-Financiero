from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from typing import Callable, Iterable

from .models import CompanyContext


# Conceptos SEC usados por el fallback selectivo. El orden implica prioridad.
CONCEPTS = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
        ("us-gaap", "Revenues", "USD"),
        ("us-gaap", "SalesRevenueNet", "USD"),
        ("ifrs-full", "Revenue", "USD"),
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss", "USD"),
        ("us-gaap", "ProfitLoss", "USD"),
        ("ifrs-full", "ProfitLoss", "USD"),
    ],
    "cfo": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities", "USD"),
    ],
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
        ("us-gaap", "PaymentsForAdditionsToPropertyPlantAndEquipment", "USD"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets", "USD"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "USD"),
    ],
    "buybacks": [
        ("us-gaap", "PaymentsForRepurchaseOfCommonStock", "USD"),
        ("us-gaap", "PaymentsForRepurchaseOfCommonAndPreferredStock", "USD"),
    ],
    "diluted_shares": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
    ],
    "shares_outstanding": [
        ("dei", "EntityCommonStockSharesOutstanding", "shares"),
        ("us-gaap", "CommonStockSharesOutstanding", "shares"),
    ],
}


ProgressFn = Callable[[int, int, str], None] | None


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _facts_for_candidate(payload: dict, taxonomy: str, tag: str, unit: str):
    obj = payload.get("facts", {}).get(taxonomy, {}).get(tag)
    if not obj:
        return []
    units = obj.get("units", {}) or {}
    rows = units.get(unit)
    if rows is None and unit == "USD":
        # Algunas extranjeras reportan moneda funcional distinta; para DCF en precio USD
        # no conviene mezclar monedas. Por eso no hacemos fallback arbitrario de divisa.
        return []
    if rows is None:
        return []

    out = []
    for raw in rows:
        try:
            val = float(raw.get("val"))
        except Exception:
            continue
        start = _parse_date(raw.get("start"))
        end = _parse_date(raw.get("end"))
        filed = _parse_date(raw.get("filed"))
        form = str(raw.get("form") or "")
        if not end:
            continue
        out.append({
            "start": start,
            "end": end,
            "filed": filed,
            "form": form,
            "val": val,
            "fy": raw.get("fy"),
            "fp": raw.get("fp"),
            "frame": raw.get("frame"),
            "tag": tag,
            "taxonomy": taxonomy,
        })
    return out


def _best_rows(payload: dict, metric: str):
    """
    Elige un solo tag por métrica para evitar mezclar definiciones.
    Preferimos el primer tag de la lista que tenga cobertura reciente razonable.
    """
    best = []
    best_score = (-1, None)
    for taxonomy, tag, unit in CONCEPTS[metric]:
        rows = _facts_for_candidate(payload, taxonomy, tag, unit)
        if not rows:
            continue
        recent_end = max((r["end"] for r in rows if r["end"]), default=None)
        score = (len(rows), recent_end)
        if score > best_score:
            best_score = score
            best = rows
    return best


def _dedupe_periods(rows):
    """Mantiene la versión presentada más recientemente de cada contexto start/end."""
    by_key = {}
    for row in rows:
        key = (row.get("start"), row.get("end"))
        old = by_key.get(key)
        if old is None or (row.get("filed") or row["end"]) >= (old.get("filed") or old["end"]):
            by_key[key] = row
    return list(by_key.values())


def _duration_days(row):
    if row.get("start") is None or row.get("end") is None:
        return None
    return (row["end"] - row["start"]).days + 1


def _classify_cumulative(days: int | None):
    if days is None:
        return None
    if 55 <= days <= 125:
        return "q1"
    if 126 <= days <= 225:
        return "q2"
    if 226 <= days <= 325:
        return "q3"
    if 326 <= days <= 430:
        return "fy"
    return None


def standalone_quarters(rows):
    """
    Convierte facts de duración en quarters standalone.

    Es especialmente importante para CFO, CapEx y buybacks, porque 10-Q suele
    reportarlos acumulados YTD:

      Q1 = Q1 YTD
      Q2 = H1 YTD - Q1 YTD
      Q3 = 9M YTD - H1 YTD
      Q4 = FY - 9M YTD

    También conserva cualquier fact trimestral directo de ~91 días.
    Devuelve {quarter_end_date: value}.
    """
    rows = [r for r in _dedupe_periods(rows) if r.get("form") in {
        "10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"
    }]

    direct = {}
    for r in rows:
        days = _duration_days(r)
        if days is not None and 55 <= days <= 125:
            end = r["end"]
            old = direct.get(end)
            if old is None or (r.get("filed") or end) >= (old.get("filed") or old["end"]):
                direct[end] = r

    # Agrupar acumulados por inicio de ejercicio fiscal.
    groups = defaultdict(list)
    for r in rows:
        if r.get("start") is not None:
            groups[r["start"]].append(r)

    derived = {}
    for start, group in groups.items():
        buckets = {}
        for r in group:
            bucket = _classify_cumulative(_duration_days(r))
            if bucket is None:
                continue
            old = buckets.get(bucket)
            if old is None or r["end"] > old["end"] or (
                r["end"] == old["end"] and (r.get("filed") or r["end"]) >= (old.get("filed") or old["end"])
            ):
                buckets[bucket] = r

        q1 = buckets.get("q1")
        q2 = buckets.get("q2")
        q3 = buckets.get("q3")
        fy = buckets.get("fy")

        if q1:
            derived[q1["end"]] = q1["val"]
        if q1 and q2:
            derived[q2["end"]] = q2["val"] - q1["val"]
        if q2 and q3:
            derived[q3["end"]] = q3["val"] - q2["val"]
        if q3 and fy:
            derived[fy["end"]] = fy["val"] - q3["val"]

    # Directo gana cuando existe; derivado completa Q2/Q3/Q4 cuando falta.
    out = dict(derived)
    for end, row in direct.items():
        out[end] = row["val"]
    return dict(sorted(out.items()))


def annual_values(rows):
    """Últimos valores FY standalone para fallback de DCF."""
    candidates = []
    for r in _dedupe_periods(rows):
        days = _duration_days(r)
        if days is not None and 326 <= days <= 430 and r.get("form") in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}:
            candidates.append(r)
    candidates.sort(key=lambda r: (r["end"], r.get("filed") or r["end"]))
    return [(r["end"], r["val"]) for r in candidates]


def instant_values(rows):
    vals = {}
    for r in _dedupe_periods(rows):
        if r.get("start") is None:
            vals[r["end"]] = r["val"]
    return dict(sorted(vals.items()))


def _nearest_value(mapping: dict, target, max_days=45):
    if not mapping or target is None:
        return None
    if target in mapping:
        return mapping[target]
    best = None
    best_delta = None
    for d, v in mapping.items():
        delta = abs((d - target).days)
        if delta <= max_days and (best_delta is None or delta < best_delta):
            best_delta = delta
            best = v
    return best


def _align(mapping: dict, periods: list):
    return [_nearest_value(mapping, p, max_days=14) for p in periods]


def _coverage(values):
    return sum(v is not None for v in values or [])


def extract_companyfacts_context(ctx: CompanyContext, payload: dict) -> CompanyContext:
    """
    Reconstruye una vista fiscal de 4 quarters usando CompanyFacts.
    Si CompanyFacts mejora la cobertura, reemplaza el contexto de Frames.
    """
    metric_rows = {m: _best_rows(payload, m) for m in CONCEPTS}
    qmaps = {
        m: standalone_quarters(metric_rows[m])
        for m in ("revenue", "net_income", "cfo", "capex", "buybacks", "diluted_shares")
    }
    outstanding_map = instant_values(metric_rows["shares_outstanding"])

    # FCF directo calculado con quarters standalone reconstruidos.
    fcf_map = {}
    for end in sorted(set(qmaps["cfo"]) | set(qmaps["capex"])):
        ocf = _nearest_value(qmaps["cfo"], end, 7)
        capex = _nearest_value(qmaps["capex"], end, 7)
        if ocf is not None and capex is not None:
            fcf_map[end] = float(ocf) - abs(float(capex))

    # Elegir calendario fiscal: revenue suele ser la serie trimestral más completa.
    period_candidates = []
    for key in ("revenue", "net_income", "cfo", "capex"):
        ends = sorted(qmaps[key])
        if len(ends) >= 4:
            period_candidates = ends[-4:]
            break
    if not period_candidates:
        union = sorted(set().union(*(set(qmaps[k]) for k in qmaps), set(fcf_map)))
        period_candidates = union[-4:]

    if len(period_candidates) < 4:
        # Aunque no podamos reconstruir 4Q, guardar metadatos de FY para DCF.
        metadata = dict(ctx.metadata or {})
        _attach_annual_fallback(metadata, metric_rows)
        return replace(ctx, metadata=metadata)

    periods = period_candidates[-4:]
    revenue = _align(qmaps["revenue"], periods)
    net_income = _align(qmaps["net_income"], periods)
    cfo = _align(qmaps["cfo"], periods)
    capex = [abs(v) if v is not None else None for v in _align(qmaps["capex"], periods)]
    buybacks = [abs(v) if v is not None else None for v in _align(qmaps["buybacks"], periods)]
    diluted = _align(qmaps["diluted_shares"], periods)

    # Rellenar shares faltantes con outstanding shares cercano al quarter end.
    for i, p in enumerate(periods):
        if diluted[i] is None:
            diluted[i] = _nearest_value(outstanding_map, p, max_days=75)

    fcf = _align(fcf_map, periods)

    metadata = dict(ctx.metadata or {})
    metadata.update({
        "source": "SEC CompanyFacts selective fallback",
        "companyfacts_fiscal_quarters": [str(p) for p in periods],
        "companyfacts_enriched": True,
    })

    latest_share = next((v for v in reversed(diluted) if v is not None), None)
    if latest_share is not None:
        metadata["shares_outstanding"] = latest_share

    if all(v is not None for v in fcf):
        metadata["ttm_fcf"] = sum(float(v) for v in fcf)
        metadata["ttm_fcf_source"] = "4 reconstructed CompanyFacts quarters"

    _attach_annual_fallback(metadata, metric_rows)

    # Sólo usar la versión CompanyFacts si no empeora drásticamente la cobertura
    # de revenue/net income y sí mejora FCF o shares.
    old_score = _coverage(ctx.fcf) * 3 + _coverage(ctx.revenue) + _coverage(ctx.net_income) + _coverage(ctx.diluted_shares)
    new_score = _coverage(fcf) * 3 + _coverage(revenue) + _coverage(net_income) + _coverage(diluted)

    if new_score < old_score and _coverage(fcf) <= _coverage(ctx.fcf):
        return replace(ctx, metadata=metadata)

    return CompanyContext(
        market=ctx.market,
        symbol=ctx.symbol,
        name=ctx.name,
        quarters=[str(p) for p in periods],
        revenue=revenue,
        net_income=net_income,
        cfo=cfo,
        capex=capex,
        fcf=fcf,
        buybacks=buybacks,
        diluted_shares=diluted,
        price=ctx.price,
        currency=ctx.currency,
        metadata=metadata,
    )


def _attach_annual_fallback(metadata: dict, metric_rows: dict):
    annual_cfo = annual_values(metric_rows.get("cfo", []))
    annual_capex = annual_values(metric_rows.get("capex", []))
    if not annual_cfo or not annual_capex:
        return

    # Buscar el FY más reciente compartido (tolerancia 14 días).
    best = None
    for cfo_end, cfo_val in reversed(annual_cfo):
        capex_val = _nearest_value(dict(annual_capex), cfo_end, max_days=14)
        if capex_val is not None:
            best = (cfo_end, float(cfo_val) - abs(float(capex_val)))
            break

    if best is not None:
        end, fcf = best
        metadata["latest_fy_fcf"] = fcf
        metadata["latest_fy_fcf_end"] = str(end)
        metadata["latest_fy_fcf_source"] = "SEC CompanyFacts latest FY OCF - CapEx"


def enrich_contexts_selectively(
    contexts: list[CompanyContext],
    client,
    predicate: Callable[[CompanyContext], bool],
    priority: Callable[[CompanyContext], float] | None = None,
    max_companies: int = 750,
    max_workers: int = 4,
    progress: ProgressFn = None,
):
    """
    Segundo pase sólo sobre compañías donde la información extra puede cambiar
    el resultado. Respeta el límite de selección y delega rate limiting al cliente SEC.
    """
    selected = [ctx for ctx in contexts if predicate(ctx)]
    if priority is not None:
        selected.sort(key=priority, reverse=True)
    selected = selected[:max_companies]
    if not selected:
        return contexts, {"selected": 0, "enriched": 0, "fcf_complete": 0}

    by_symbol = {ctx.symbol: ctx for ctx in contexts}
    enriched = 0
    fcf_complete = 0
    done = 0

    def fetch(ctx):
        cik = (ctx.metadata or {}).get("cik")
        if cik is None:
            return ctx.symbol, None
        payload = client.companyfacts(int(cik))
        if not payload:
            return ctx.symbol, None
        return ctx.symbol, extract_companyfacts_context(ctx, payload)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as ex:
        futures = {ex.submit(fetch, ctx): ctx.symbol for ctx in selected}
        for fut in as_completed(futures):
            symbol = futures[fut]
            try:
                _, new_ctx = fut.result()
            except Exception:
                new_ctx = None
            if new_ctx is not None:
                by_symbol[symbol] = new_ctx
                enriched += 1
                if _coverage(new_ctx.fcf) == 4:
                    fcf_complete += 1
            done += 1
            if progress and (done == len(selected) or done % 10 == 0):
                progress(done, len(selected), f"CompanyFacts: {done}/{len(selected)} · enriquecidas: {enriched} · FCF 4Q: {fcf_complete}")

    return [by_symbol[ctx.symbol] for ctx in contexts], {
        "selected": len(selected),
        "enriched": enriched,
        "fcf_complete": fcf_complete,
    }

from .criteria import CRITERIA, MIN_CRITERIA_TO_SURFACE, intrinsic_value_per_share
from .models import CompanyContext, ScreenedCompany

def evaluate_company(ctx: CompanyContext):
    criteria = [
        item.evaluator(ctx)
        for item in CRITERIA
    ]

    matches = sum(
        int(c.passed)
        for c in criteria
    )

    intrinsic, dcf_reason = (
        intrinsic_value_per_share(ctx)
    )

    # Todas las compañías —USA y ARG— dejan un estado DCF auditable.
    ctx.metadata = dict(
        ctx.metadata or {}
    )

    existing_status = ctx.metadata.get(
        "dcf_quality_status"
    )

    if intrinsic is not None:
        # Normalizar el estado exitoso para que la UI sea consistente
        # entre USA y Argentina.
        if (
            not existing_status
            or str(existing_status).lower() == "ok"
        ):
            ctx.metadata[
                "dcf_quality_status"
            ] = "OK"
    else:
        if not existing_status:
            ctx.metadata[
                "dcf_quality_status"
            ] = "N/D"

    ctx.metadata[
        "dcf_reason"
    ] = dcf_reason

    upside = None
    if (
        intrinsic is not None
        and ctx.price is not None
        and ctx.price > 0
    ):
        upside = (
            intrinsic / ctx.price
            - 1.0
        )

    return ScreenedCompany(
        ctx.market,
        ctx.symbol,
        ctx.name,
        criteria,
        matches,
        matches >= MIN_CRITERIA_TO_SURFACE,
        intrinsic,
        ctx.price,
        upside,
        ctx.metadata,
    )

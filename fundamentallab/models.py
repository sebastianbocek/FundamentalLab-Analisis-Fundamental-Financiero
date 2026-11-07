from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass
class CriterionResult:
    key: str
    label: str
    passed: bool
    value: Any = None
    reason: str = ""
    # False = no fue posible evaluarlo por falta de datos. No equivale a rojo.
    evaluable: bool = True

@dataclass
class CompanyContext:
    market: str
    symbol: str
    name: str
    quarters: list[str]
    revenue: list[float | None]
    net_income: list[float | None]
    cfo: list[float | None]
    capex: list[float | None]
    fcf: list[float | None]
    buybacks: list[float | None]
    diluted_shares: list[float | None]
    price: float | None = None
    currency: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class ScreenedCompany:
    market: str
    symbol: str
    name: str
    criteria: list[CriterionResult]
    matches: int
    surfaced: bool
    intrinsic_value: float | None = None
    price: float | None = None
    upside_to_intrinsic: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

"""Carga de reglas versionadas; el hash del archivo forma parte de cada resultado."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_RULES = Path(__file__).resolve().parents[2] / "rules" / "rules_v1.yaml"


@dataclass(frozen=True)
class RuleSet:
    version: str
    sha256: str
    currencies: tuple[str, ...]
    qty_units: int
    unit_price_bp: int
    unit_price_abs_minor: int
    tax_abs_minor: int
    total_abs_minor: int
    future_days_allowed: int
    approval_status: str
    approved_by: str | None
    path: str
    line_amount_cap_minor: tuple[tuple[str, int], ...] = ()   # (moneda, tope); vacío = sin tope
    approved_on: str | None = None

    def price_tolerance(self, po_unit_minor: int) -> int:
        return max(self.unit_price_abs_minor, po_unit_minor * self.unit_price_bp // 10_000)

    def line_cap(self, currency: str) -> int | None:
        return dict(self.line_amount_cap_minor).get(currency)


def _non_negative_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def load_rules(path: str | Path = DEFAULT_RULES) -> RuleSet:
    p = Path(path)
    raw = p.read_bytes()
    cfg = yaml.safe_load(raw)
    tol, dates, appr = cfg["tolerances"], cfg["dates"], cfg.get("approval") or {}
    for k in ("qty_units", "unit_price_bp", "unit_price_abs_minor", "tax_abs_minor", "total_abs_minor"):
        if not _non_negative_int(tol[k]):
            raise ValueError(f"tolerancia inválida: {k}")
    caps = tol.get("line_amount_cap_minor") or {}
    if not isinstance(caps, dict) or any(c not in cfg["currencies"] or not _non_negative_int(v) for c, v in caps.items()):
        raise ValueError("tope por línea inválido: solo monedas admitidas y enteros no negativos")
    return RuleSet(
        version=str(cfg["version"]), sha256=hashlib.sha256(raw).hexdigest(),
        currencies=tuple(cfg["currencies"]),
        qty_units=tol["qty_units"], unit_price_bp=tol["unit_price_bp"],
        unit_price_abs_minor=tol["unit_price_abs_minor"], tax_abs_minor=tol["tax_abs_minor"],
        total_abs_minor=tol["total_abs_minor"], future_days_allowed=int(dates["future_days_allowed"]),
        approval_status=str(appr.get("status", "pending")), approved_by=appr.get("approved_by"),
        path=p.name, line_amount_cap_minor=tuple(sorted(caps.items())),
        approved_on=None if appr.get("approved_on") is None else str(appr["approved_on"]),
    )

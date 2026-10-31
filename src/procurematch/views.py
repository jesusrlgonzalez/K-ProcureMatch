"""Filtros e indicadores compartidos por el dashboard y la API: una sola lógica, mismos totales."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .engine import summarize


@dataclass
class Filters:
    suppliers: list[str] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)          # al menos uno de los motivos
    review_states: list[str] = field(default_factory=list)    # PENDIENTE / RESUELTO
    date_from: date | None = None
    date_to: date | None = None
    include_undated: bool = True   # facturas con fecha inválida no desaparecen al filtrar por periodo

    def as_dict(self) -> dict:
        d = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in self.__dict__.items()
             if v and k != "include_undated"}
        if (self.date_from or self.date_to) and not self.include_undated:
            d["include_undated"] = False
        return d


def apply(cases: pd.DataFrame, f: Filters) -> pd.DataFrame:
    m = pd.Series(True, index=cases.index)
    if f.suppliers:
        m &= cases["supplier_id"].isin(f.suppliers)
    if f.currencies:
        m &= cases["currency"].isin(f.currencies)
    if f.statuses:
        m &= cases["status"].isin(f.statuses)
    if f.review_states and "review_state" in cases:
        m &= cases["review_state"].isin(f.review_states)
    if f.reasons:
        codes = cases["reasons"].fillna("").str.split(";")
        m &= codes.apply(lambda xs: bool(set(xs) & set(f.reasons)))
    if f.date_from or f.date_to:
        d = pd.to_datetime(cases["invoice_date"], errors="coerce", format="%Y-%m-%d").dt.date
        in_range = pd.Series(True, index=cases.index)
        if f.date_from:
            in_range &= d.notna() & (d >= f.date_from)
        if f.date_to:
            in_range &= d.notna() & (d <= f.date_to)
        m &= in_range | (d.isna() & f.include_undated)
    return cases[m]


def kpis(cases: pd.DataFrame) -> dict:
    s = summarize(cases)
    total = s["lineas"]
    pending = int((cases.get("review_state", pd.Series(dtype=str)) == "PENDIENTE").sum())
    need_human = total - s["por_estado"]["MATCH"]
    s.update(
        tasa_conciliacion=round(s["por_estado"]["MATCH"] / total, 4) if total else 0.0,
        requieren_analista=need_human,
        esfuerzo_revision=round(need_human / total, 4) if total else 0.0,
        pendientes_de_decision=pending,
    )
    return s


def reason_catalog(cases: pd.DataFrame) -> list[str]:
    return sorted({x for r in cases["reasons"].fillna("") for x in r.split(";") if x})

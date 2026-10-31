"""Filtros e indicadores con pandas: una sola lógica para la API y, a través de ella, para el dashboard."""
from __future__ import annotations

import pandas as pd

from .engine import describe_reason, summarize
from .filters import Filters
from .schemas import STATUS_LABEL

__all__ = ["Filters", "apply", "kpis", "reason_catalog", "reason_counts", "facets"]


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


def reason_counts(cases: pd.DataFrame) -> list[dict]:
    """Líneas por motivo (una línea puede tener varios motivos: no es una partición)."""
    codes = cases["reasons"].fillna("").str.split(";").explode()
    counts = codes[codes != ""].value_counts()
    rows = [{"codigo": c, "descripcion": describe_reason(c), "lineas": int(n)} for c, n in counts.items()]
    return sorted(rows, key=lambda r: (-r["lineas"], r["codigo"]))


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
        motivos=reason_counts(cases),
    )
    return s


def reason_catalog(cases: pd.DataFrame) -> list[str]:
    return sorted({x for r in cases["reasons"].fillna("") for x in r.split(";") if x})


def facets(cases: pd.DataFrame) -> dict:
    """Valores disponibles para cada filtro de la corrida."""
    def distinct(col: str) -> list[str]:
        return sorted({str(v) for v in cases[col].dropna() if str(v)})

    dates = pd.to_datetime(cases["invoice_date"], errors="coerce", format="%Y-%m-%d").dropna()
    return {
        "proveedores": distinct("supplier_id"),
        "monedas": distinct("currency"),
        "estados": [{"codigo": s.value, "etiqueta": lbl} for s, lbl in STATUS_LABEL.items()],
        "motivos": [{"codigo": c, "descripcion": describe_reason(c)} for c in reason_catalog(cases)],
        "revision": ["PENDIENTE", "RESUELTO"],
        "fecha_min": dates.min().date().isoformat() if len(dates) else None,
        "fecha_max": dates.max().date().isoformat() if len(dates) else None,
        "facturas_sin_fecha_valida": int(len(cases) - len(dates)),
    }

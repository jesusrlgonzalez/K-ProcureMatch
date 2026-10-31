"""Capa de servicio común a la API y al dashboard."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import REASON_TEXT, RunResult, run
from .export import evidence_zip
from .rules import DEFAULT_RULES
from .schemas import Resolution
from .store import Store
from .views import Filters, apply, kpis

REF_RE = re.compile(r"^(?P<file>[^:]+):fila (?P<row>\d+)$")


class StaleRun(RuntimeError):
    """Las fuentes cambiaron desde la corrida: el resultado ya no es reproducible con ellas."""


class Service:
    def __init__(self, data_dir: str | Path, db_path: str | Path = ":memory:", rules_path: str | Path = DEFAULT_RULES,
                 files: dict[str, Path] | None = None):
        self.data_dir, self.rules_path, self.files = Path(data_dir), Path(rules_path), files
        self.store = Store(db_path)
        self._cache: dict[str, RunResult] = {}

    def new_run(self, as_of: date | None = None) -> RunResult:
        res = run(self.data_dir, as_of=as_of, rules_path=self.rules_path, files=self.files)
        self.store.save_run(res)
        self._cache[res.run_id] = res
        return res

    def result(self, run_id: str) -> RunResult:
        if run_id not in self._cache:
            man = self.store.manifest(run_id)
            res = run(self.data_dir, as_of=date.fromisoformat(man["as_of"]), rules_path=self.rules_path,
                      files=self.files)
            if res.run_id != run_id:
                raise StaleRun(run_id)
            self._cache[run_id] = res
        return self._cache[run_id]

    def cases(self, run_id: str, filters: Filters | None = None) -> pd.DataFrame:
        df = self.store.cases(run_id)
        return apply(df, filters) if filters else df

    def summary(self, run_id: str, filters: Filters | None = None) -> dict:
        return kpis(self.cases(run_id, filters))

    def resolve(self, run_id: str, case_id: str, res: Resolution) -> dict:
        return self.store.resolve(run_id, case_id, res)

    def export(self, run_id: str, filters: Filters | None = None) -> bytes:
        res = self.result(run_id)
        cases = self.cases(run_id, filters)
        issues = res.ingested.issues
        return evidence_zip(self.store.manifest(run_id), cases, issues, res.ingested.coverage,
                            filters.as_dict() if filters else None)

    def case_detail(self, run_id: str, case_id: str) -> dict:
        cases = self.store.cases(run_id)
        hit = cases[cases["case_id"] == case_id]
        if hit.empty:
            raise KeyError(case_id)
        c = _clean(hit.iloc[0].to_dict())
        res = self.result(run_id)
        docs = {"factura": [self._raw(res, c["invoice_ref"])],
                "orden": [self._raw(res, c["po_ref"])] if c.get("po_ref") else [],
                "recepciones": [self._raw(res, x) for x in str(c.get("receipt_refs") or "").split(";") if x]}
        reasons = [{"codigo": x, "descripcion": REASON_TEXT.get(x.split(":")[-1], "Validación de formato")}
                   for x in str(c.get("reasons") or "").split(";") if x]
        notes = [{"codigo": x, "descripcion": REASON_TEXT.get(x, x)} for x in str(c.get("notes") or "").split(";") if x]
        return {"caso": c, "documentos": docs, "comparacion": comparison(c), "motivos": reasons, "notas": notes,
                "incidencias": res.ingested.issues[(res.ingested.issues["file"] == "invoice_lines.csv")
                                                   & (res.ingested.issues["row_no"] == c["row_no"])]
                .to_dict("records")}

    @staticmethod
    def _raw(res: RunResult, ref: str) -> dict:
        m = REF_RE.match(ref)
        t = next(t for t in res.ingested.tables.values() if t.file == m["file"])
        row = t.raw[t.raw["row_no"] == int(m["row"])].iloc[0].to_dict()
        return {"fuente": ref, **{k: v for k, v in row.items() if k != "row_no"}}


def comparison(c: dict) -> list[dict]:
    """Tabla campo a campo de los tres documentos con la diferencia calculada por el motor."""
    def g(k):
        return c.get(k)

    def same(a, b, word):
        return None if a is None or a == b else word

    def row(campo, orden=None, recepcion=None, factura=None, esperado=None, diferencia=None, tolerancia=None):
        return {"campo": campo, "orden": orden, "recepcion": recepcion, "factura": factura, "esperado": esperado,
                "diferencia": diferencia, "tolerancia": tolerancia}

    rate = g("po_tax_rate_bp")
    return [
        row("Proveedor", g("po_supplier_id"), None, g("supplier_id"),
            diferencia=same(g("po_supplier_id"), g("supplier_id"), "distinto")),
        row("SKU", g("po_sku"), None, g("sku"), diferencia=same(g("po_sku"), g("sku"), "distinto")),
        row("Moneda", g("po_currency"), None, g("currency"), diferencia=same(g("po_currency"), g("currency"), "distinta")),
        row("Cantidad", g("po_qty_ordered"), g("qty_received_net"), g("qty_billed"), esperado=g("qty_billable"),
            diferencia=g("qty_diff")),
        row("Precio unitario", g("po_unit_price_minor"), None, g("unit_price_minor"), esperado=g("po_unit_price_minor"),
            diferencia=g("price_diff_minor"), tolerancia=g("price_tolerance_minor")),
        row("Impuesto", f"{rate / 100:.2f} %" if rate is not None else None, None, g("tax_minor"),
            esperado=g("tax_expected_minor"), diferencia=g("tax_diff_minor")),
        row("Total", None, None, g("total_minor"), esperado=g("amount_expected_minor"), diferencia=g("amount_diff_minor")),
        row("Fecha", g("po_date"), None, g("invoice_date")),
    ]


def _clean(d: dict) -> dict:
    """Valores de pandas/numpy → tipos JSON (NA → None, enteros numpy → int)."""
    out = {}
    for k, v in d.items():
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            out[k] = None
        elif isinstance(v, np.integer) or (isinstance(v, float) and v.is_integer()):
            out[k] = int(v)  # DuckDB devuelve enteros con nulos como float
        else:
            out[k] = v
    return out

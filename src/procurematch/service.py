"""Capa de servicio detrás de la API. El dashboard no la usa directamente: consume la API por HTTP."""
from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import RunResult, describe_reason, run
from .export import evidence_zip
from .ingest import REQUIRED, TABLES, SchemaError, load
from .rules import DEFAULT_RULES, load_rules
from .schemas import Resolution
from .store import Store
from .views import Filters, apply, facets, kpis

REF_RE = re.compile(r"^(?P<file>[^:]+):fila (?P<row>\d+)$")
DATASET_RE = re.compile(r"^[0-9a-f]{16}$")
DEMO = "demo"


class StaleRun(RuntimeError):
    """Las fuentes cambiaron desde la corrida: el resultado ya no es reproducible con ellas."""


class UnknownDataset(KeyError):
    pass


class Service:
    MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # por archivo

    def __init__(self, data_dir: str | Path, db_path: str | Path = ":memory:", rules_path: str | Path = DEFAULT_RULES,
                 upload_dir: str | Path | None = None):
        self.data_dir, self.rules_path = Path(data_dir), Path(rules_path)
        self._upload_dir = Path(upload_dir) if upload_dir else None
        self.store = Store(db_path)
        self._cache: dict[str, RunResult] = {}

    @property
    def upload_dir(self) -> Path:
        if self._upload_dir is None:  # se crea solo si alguien sube archivos
            self._upload_dir = Path(tempfile.mkdtemp(prefix="kpm_uploads_"))
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        return self._upload_dir

    # ------------------------------------------------------------ fuentes
    def dataset_dir(self, dataset_id: str) -> Path:
        if dataset_id == DEMO:
            return self.data_dir
        if self._upload_dir is None or not DATASET_RE.match(dataset_id or "") \
                or not (self._upload_dir / dataset_id).is_dir():
            raise UnknownDataset(dataset_id)
        return self._upload_dir / dataset_id

    def add_dataset(self, files: dict[str, bytes]) -> dict:
        """Guarda un juego de fuentes subido y valida su esquema. El identificador es el hash del contenido."""
        unknown = set(files) - set(TABLES)
        if unknown:
            raise SchemaError(f"fuentes desconocidas: {sorted(unknown)}")
        missing = [n for n in REQUIRED if not files.get(n)]
        if missing:
            raise SchemaError(f"faltan fuentes obligatorias: {[TABLES[n][0] for n in missing]}")
        for name, data in files.items():
            if len(data) > self.MAX_UPLOAD_BYTES:
                raise ValueError(f"{TABLES[name][0]} supera {self.MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        digest = hashlib.sha256()
        for name in sorted(files):
            digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
        dataset_id = digest.hexdigest()[:16]
        target = self.upload_dir / dataset_id
        tmp = Path(tempfile.mkdtemp(prefix=f"{dataset_id}_", dir=self.upload_dir))
        try:
            for name, data in files.items():
                if data:
                    (tmp / TABLES[name][0]).write_bytes(data)
            ing = load(tmp)  # valida columnas y lectura antes de aceptar el juego de datos
            if target.exists():
                shutil.rmtree(tmp)
            else:
                tmp.rename(target)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return {"dataset_id": dataset_id, "fuentes": ing.sources()}

    # ------------------------------------------------------------ corridas
    def new_run(self, as_of: date | None = None, dataset_id: str = DEMO) -> RunResult:
        res = run(self.dataset_dir(dataset_id), as_of=as_of, rules_path=self.rules_path)
        self.store.save_run(res, dataset_id)
        self._cache[res.run_id] = res
        return res

    def result(self, run_id: str) -> RunResult:
        if run_id not in self._cache:
            man = self.store.manifest(run_id)
            try:
                src = self.dataset_dir(self.store.dataset_of(run_id))
            except UnknownDataset:
                raise StaleRun(run_id) from None
            res = run(src, as_of=date.fromisoformat(man["as_of"]), rules_path=self.rules_path)
            if res.run_id != run_id:
                raise StaleRun(run_id)
            self._cache[run_id] = res
        return self._cache[run_id]

    def rules(self) -> dict:
        r = load_rules(self.rules_path)
        return {"version": r.version, "sha256": r.sha256, "archivo": r.path,
                "aprobacion": {"estado": r.approval_status, "aprobado_por": r.approved_by},
                "monedas": list(r.currencies),
                "tolerancias": {"cantidad_unidades": r.qty_units, "precio_bp": r.unit_price_bp,
                                "precio_abs_minor": r.unit_price_abs_minor, "impuesto_abs_minor": r.tax_abs_minor,
                                "total_abs_minor": r.total_abs_minor},
                "dias_futuros_permitidos": r.future_days_allowed}

    # ------------------------------------------------------------ consultas
    def cases(self, run_id: str, filters: Filters | None = None) -> pd.DataFrame:
        df = self.store.cases(run_id)
        return apply(df, filters) if filters else df

    def summary(self, run_id: str, filters: Filters | None = None) -> dict:
        return kpis(self.cases(run_id, filters))

    def facets(self, run_id: str) -> dict:
        return facets(self.store.cases(run_id))

    def data_quality(self, run_id: str) -> dict:
        ing = self.result(run_id).ingested
        return {"cobertura": _records(ing.coverage), "incidencias": _records(ing.issues)}

    def resolve(self, run_id: str, case_id: str, res: Resolution) -> dict:
        return self.store.resolve(run_id, case_id, res)

    def export(self, run_id: str, filters: Filters | None = None) -> bytes:
        res = self.result(run_id)
        return evidence_zip(self.store.manifest(run_id), self.cases(run_id, filters), res.ingested.issues,
                            res.ingested.coverage, filters.as_dict() if filters else None)

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
        reasons = [{"codigo": x, "descripcion": describe_reason(x)} for x in str(c.get("reasons") or "").split(";") if x]
        notes = [{"codigo": x, "descripcion": describe_reason(x)} for x in str(c.get("notes") or "").split(";") if x]
        issues = res.ingested.issues
        return {"caso": c, "documentos": docs, "comparacion": comparison(c), "motivos": reasons, "notas": notes,
                "incidencias": _records(issues[(issues["file"] == "invoice_lines.csv")
                                               & (issues["row_no"] == c["row_no"])])}

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
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _records(df: pd.DataFrame) -> list[dict]:
    return [_clean(r) for r in df.to_dict("records")]

"""Ingesta con DuckDB: lee los CSV como texto, valida cada fila y mide cobertura.

Ninguna fila se elimina: las inválidas quedan registradas por archivo, fila, campo y código.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from pydantic import BaseModel, ValidationError

from .schemas import GoodsReceipt, InvoiceLine, PurchaseOrderLine, Supplier

TABLES: dict[str, tuple[str, type[BaseModel], tuple[str, ...]]] = {
    # nombre lógico: (archivo, modelo, columnas obligatorias)
    "purchase_orders": ("purchase_orders.csv", PurchaseOrderLine,
                        ("po_id", "po_line", "supplier_id", "sku", "qty_ordered", "unit_price_minor",
                         "tax_rate_bp", "currency", "po_date")),
    "goods_receipts": ("goods_receipts.csv", GoodsReceipt,
                       ("receipt_id", "po_id", "po_line", "sku", "qty_received", "receipt_date")),
    "invoice_lines": ("invoice_lines.csv", InvoiceLine,
                      ("invoice_id", "invoice_line", "supplier_id", "po_id", "sku", "invoice_date",
                       "qty_billed", "unit_price_minor", "tax_minor", "total_minor", "currency",
                       "document_type")),
    "suppliers": ("suppliers.csv", Supplier, ("supplier_id",)),  # maestro opcional
}
REQUIRED = ("purchase_orders", "goods_receipts", "invoice_lines")
DATE_COL = {"purchase_orders": "po_date", "goods_receipts": "receipt_date", "invoice_lines": "invoice_date"}


class SchemaError(ValueError):
    """El archivo no cumple el esquema documentado (columnas faltantes o archivo ausente)."""


@dataclass
class Table:
    name: str
    file: str
    sha256: str
    raw: pd.DataFrame                  # texto original, con row_no
    valid: list = field(default_factory=list)
    invalid_rows: set[int] = field(default_factory=set)


@dataclass
class Ingested:
    tables: dict[str, Table]
    issues: pd.DataFrame               # file, row_no, field, code, value
    coverage: pd.DataFrame

    def sources(self) -> dict[str, dict]:
        return {t.file: {"sha256": t.sha256, "rows": len(t.raw)} for t in self.tables.values()}


def _read(path: Path) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT * FROM read_csv(?, header=true, all_varchar=true, auto_detect=true, "
            "null_padding=true, quote='\"')", [str(path)]).df()
    finally:
        con.close()
    df = df.fillna("").map(lambda v: v if isinstance(v, str) else str(v))
    df.columns = [c.strip() for c in df.columns]
    df.insert(0, "row_no", range(1, len(df) + 1))
    return df


def _code(err: dict) -> str:
    msg = str(err.get("msg", ""))
    for token in ("MISSING_KEY", "INVALID_ID_FORMAT", "INVALID_NUMBER", "NEGATIVE_NOT_ALLOWED", "INVALID_DATE",
                  "INVALID_CURRENCY", "INVALID_DOCUMENT_TYPE", "INVALID_MOVEMENT"):
        if token in msg:
            return token
    return "INVALID_VALUE"


def load(source_dir: str | Path, files: dict[str, Path] | None = None) -> Ingested:
    """Carga las tablas desde `source_dir` (o rutas explícitas en `files`, p. ej. subidas desde la interfaz)."""
    source_dir = Path(source_dir)
    tables, issues = {}, []
    for name, (fname, model, required) in TABLES.items():
        path = Path(files[name]) if files and name in files else source_dir / fname
        if not path.is_file():
            if name in REQUIRED:
                raise SchemaError(f"Falta la fuente obligatoria {fname}")
            continue
        raw = _read(path)
        missing = [c for c in required if c not in raw.columns]
        if missing:
            raise SchemaError(f"{fname}: faltan columnas {missing}")
        t = Table(name, fname, hashlib.sha256(path.read_bytes()).hexdigest(), raw)
        for rec in raw.to_dict("records"):
            try:
                t.valid.append(model(**rec))
            except ValidationError as e:
                t.invalid_rows.add(rec["row_no"])
                for err in e.errors():
                    col = str(err["loc"][0]) if err.get("loc") else ""
                    issues.append({"file": fname, "row_no": rec["row_no"], "field": col, "code": _code(err),
                                   "value": str(rec.get(col, ""))[:80]})
        tables[name] = t

    _flag_duplicates(tables, issues)
    issues_df = pd.DataFrame(issues, columns=["file", "row_no", "field", "code", "value"])
    return Ingested(tables, issues_df, _coverage(tables, issues_df))


def _flag_duplicates(tables: dict[str, Table], issues: list[dict]) -> None:
    """Duplicados se marcan, no se eliminan; el motor decide cómo tratarlos."""
    checks = {"purchase_orders": ("po_id", "po_line"), "goods_receipts": ("receipt_id",)}
    codes = {"purchase_orders": "DUPLICATE_PO_LINE", "goods_receipts": "DUPLICATE_RECEIPT_ID"}
    for name, keys in checks.items():
        t = tables.get(name)
        if not t:
            continue
        seen: dict[tuple, list[int]] = {}
        for m in t.valid:
            seen.setdefault(tuple(getattr(m, k) for k in keys), []).append(m.row_no)
        for key, rows in seen.items():
            if len(rows) > 1:
                for r in rows:
                    issues.append({"file": t.file, "row_no": r, "field": "+".join(keys), "code": codes[name],
                                   "value": "|".join(key)})


def _coverage(tables: dict[str, Table], issues: pd.DataFrame) -> pd.DataFrame:
    """Cobertura y periodos por tabla, calculados en SQL sobre las filas válidas."""
    con = duckdb.connect()
    rows = []
    try:
        for name, t in tables.items():
            valid = pd.DataFrame([m.model_dump() for m in t.valid]) if t.valid else pd.DataFrame()
            dcol = DATE_COL.get(name)
            period = (None, None)
            currencies = ""
            if dcol and not valid.empty:
                con.register("v", valid)
                period = con.execute(f"SELECT min({dcol}), max({dcol}) FROM v").fetchone()
                if "currency" in valid.columns:
                    currencies = ", ".join(r[0] for r in con.execute(
                        "SELECT DISTINCT currency FROM v ORDER BY 1").fetchall())
                con.unregister("v")
            dup = int((issues["file"].eq(t.file) & issues["code"].str.startswith("DUPLICATE")).sum()) if len(issues) else 0
            rows.append({"tabla": name, "archivo": t.file, "registros": len(t.raw), "validos": len(t.valid),
                         "invalidos": len(t.invalid_rows), "marcados_duplicado": dup,
                         "desde": str(period[0]) if period[0] else "", "hasta": str(period[1]) if period[1] else "",
                         "monedas": currencies, "sha256": t.sha256})
    finally:
        con.close()
    return pd.DataFrame(rows)

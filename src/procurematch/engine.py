"""Motor determinista de conciliación 3-way, ejecutado en SQL (DuckDB) sobre tablas pandas.

Cada línea de factura recibe exactamente un estado (MATCH, EXCEPTION, REVIEW, UNMATCHED, BLOCKED),
los códigos de motivo que lo justifican, las diferencias calculadas y la referencia exacta a la fila
de cada documento fuente. El motor nunca autoriza pagos ni registra asientos.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from . import __version__
from .ingest import Ingested, load
from .rules import DEFAULT_RULES, RuleSet, load_rules
from .schemas import STATUS_LABEL, Status

# Códigos de motivo → estado que fuerzan. El estado final es el de mayor precedencia.
REASONS: dict[str, Status] = {
    # entrada inválida
    "PO_LINE_INVALID": Status.BLOCKED, "CURRENCY_NOT_ADMITTED": Status.BLOCKED, "FUTURE_DATED": Status.BLOCKED,
    "UNKNOWN_SUPPLIER": Status.BLOCKED, "NON_POSITIVE_QTY": Status.BLOCKED, "NEGATIVE_AMOUNT": Status.BLOCKED,
    # criterio humano
    "DUPLICATE_INVOICE": Status.REVIEW, "AMBIGUOUS_PO_CANDIDATES": Status.REVIEW, "DUPLICATE_RECEIPT": Status.REVIEW,
    "CREDIT_NOTE": Status.REVIEW, "CREDIT_NOTE_WITHOUT_REFERENCE": Status.REVIEW,
    # sin vínculo
    "PO_NOT_FOUND": Status.UNMATCHED, "NO_RECEIPT": Status.UNMATCHED,
    # discrepancias documentales y monetarias
    "SUPPLIER_MISMATCH": Status.EXCEPTION, "SKU_MISMATCH": Status.EXCEPTION, "CURRENCY_MISMATCH": Status.EXCEPTION,
    "INVOICE_BEFORE_PO": Status.EXCEPTION, "PRICE_OUT_OF_TOLERANCE": Status.EXCEPTION,
    "QTY_EXCEEDS_RECEIVED": Status.EXCEPTION, "QTY_EXCEEDS_ORDERED": Status.EXCEPTION,
    "TAX_MISMATCH": Status.EXCEPTION, "TOTAL_ARITHMETIC_MISMATCH": Status.EXCEPTION,
    "AMOUNT_DIFF_ABOVE_CAP": Status.EXCEPTION,
}
NOTES = {"PRICE_WITHIN_TOLERANCE", "PARTIAL_RECEIPTS", "RETURN_APPLIED", "PARTIAL_BILLING"}
PRECEDENCE = [Status.BLOCKED, Status.REVIEW, Status.UNMATCHED, Status.EXCEPTION, Status.MATCH]

REASON_TEXT = {
    "PO_LINE_INVALID": "La línea de orden referida existe pero es inválida",
    "CURRENCY_NOT_ADMITTED": "Moneda no admitida por las reglas",
    "FUTURE_DATED": "Fecha de factura posterior a la fecha de corte",
    "UNKNOWN_SUPPLIER": "Proveedor no existe en el maestro",
    "NON_POSITIVE_QTY": "Factura con cantidad cero o negativa",
    "NEGATIVE_AMOUNT": "Factura con importe negativo",
    "DUPLICATE_INVOICE": "Misma factura (proveedor, folio, línea) aparece más de una vez",
    "AMBIGUOUS_PO_CANDIDATES": "Más de una línea de orden candidata",
    "DUPLICATE_RECEIPT": "La recepción vinculada tiene folio duplicado",
    "CREDIT_NOTE": "Nota de crédito: requiere revisión humana",
    "CREDIT_NOTE_WITHOUT_REFERENCE": "Nota de crédito sin factura original",
    "PO_NOT_FOUND": "No existe orden de compra para la referencia",
    "NO_RECEIPT": "La orden no tiene recepción que respalde la factura",
    "SUPPLIER_MISMATCH": "El proveedor de la factura no coincide con el de la orden",
    "SKU_MISMATCH": "El SKU de la factura no coincide con el de la orden",
    "CURRENCY_MISMATCH": "La moneda de la factura no coincide con la de la orden",
    "INVOICE_BEFORE_PO": "Factura fechada antes que la orden",
    "PRICE_OUT_OF_TOLERANCE": "Precio unitario fuera de tolerancia",
    "QTY_EXCEEDS_RECEIVED": "Cantidad facturada supera lo recibido no facturado",
    "QTY_EXCEEDS_ORDERED": "Cantidad facturada supera lo ordenado pendiente",
    "TAX_MISMATCH": "Impuesto distinto al calculado con la tasa de la orden",
    "TOTAL_ARITHMETIC_MISMATCH": "Total distinto de subtotal + impuesto",
    "AMOUNT_DIFF_ABOVE_CAP": "Diferencia en el total de la línea (impuestos incluidos) superior al tope de su moneda",
    "PRICE_WITHIN_TOLERANCE": "Diferencia de precio dentro de tolerancia",
    "PARTIAL_RECEIPTS": "Cantidad cubierta por varias recepciones",
    "RETURN_APPLIED": "Se descontaron devoluciones de la recepción",
    "PARTIAL_BILLING": "La orden se factura en varias facturas",
}

VALIDATION_TEXT = {
    "MISSING_KEY": "falta la clave", "INVALID_ID_FORMAT": "identificador con formato inválido",
    "INVALID_NUMBER": "número inválido", "NEGATIVE_NOT_ALLOWED": "valor negativo no permitido",
    "INVALID_DATE": "fecha inválida", "INVALID_CURRENCY": "moneda con formato inválido",
    "INVALID_DOCUMENT_TYPE": "tipo de documento inválido", "INVALID_MOVEMENT": "movimiento inválido",
    "INVALID_VALUE": "valor inválido",
}


def describe_reason(code: str) -> str:
    """Texto legible de un motivo; los de validación tienen la forma `campo:CÓDIGO`."""
    if ":" in code:
        field, err = code.split(":", 1)
        return f"Validación: {VALIDATION_TEXT.get(err, err)} en «{field}»"
    return REASON_TEXT.get(code, code)


CASE_COLUMNS = [
    "case_id", "invoice_ref", "row_no", "invoice_id", "invoice_line", "supplier_id", "po_id", "po_line", "sku",
    "invoice_date", "currency", "document_type", "qty_billed", "unit_price_minor", "tax_minor", "total_minor",
    "status", "status_label", "reasons", "notes",
    "po_ref", "po_supplier_id", "po_sku", "po_currency", "po_date", "po_qty_ordered", "po_unit_price_minor",
    "po_tax_rate_bp", "receipt_refs", "qty_received_net", "qty_billable",
    "qty_diff", "price_diff_minor", "price_tolerance_minor", "tax_expected_minor", "tax_diff_minor",
    "total_arith_diff_minor", "amount_expected_minor", "amount_diff_minor",
]

NUMERIC_COLUMNS = ["row_no", "qty_billed", "unit_price_minor", "tax_minor", "total_minor", "po_qty_ordered",
                   "po_unit_price_minor", "po_tax_rate_bp", "qty_received_net", "qty_billable", "qty_diff",
                   "price_diff_minor", "price_tolerance_minor", "tax_expected_minor", "tax_diff_minor",
                   "total_arith_diff_minor", "amount_expected_minor", "amount_diff_minor"]


@dataclass
class RunResult:
    run_id: str
    as_of: date
    rules: RuleSet
    ingested: Ingested
    cases: pd.DataFrame

    @property
    def summary(self) -> dict:
        return summarize(self.cases)

    def manifest(self) -> dict:
        return {"run_id": self.run_id, "as_of": self.as_of.isoformat(), "engine_version": __version__,
                "rules": {"version": self.rules.version, "sha256": self.rules.sha256, "file": self.rules.path,
                          "approval_status": self.rules.approval_status, "approved_by": self.rules.approved_by,
                          "approved_on": self.rules.approved_on},
                "sources": self.ingested.sources(),
                "authority": {"payments_executed": False, "erp_postings": False, "purchase_orders_created": False}}


def run(source_dir: str | Path, *, as_of: date | None = None, rules_path: str | Path = DEFAULT_RULES,
        files: dict[str, Path] | None = None) -> RunResult:
    rules = load_rules(rules_path)
    ing = load(source_dir, files)
    as_of = as_of or date.today()
    fingerprint = json.dumps({"sources": ing.sources(), "rules": rules.sha256, "as_of": as_of.isoformat(),
                              "engine": __version__}, sort_keys=True)
    run_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    return RunResult(run_id, as_of, rules, ing, reconcile(ing, rules, as_of))


SQL = Path(__file__).with_name("sql") / "reconcile.sql"

# Esquema de las tablas que consume reconcile.sql (los datos ya pasaron la validación Pydantic).
TABLE_DDL = {
    "inv_valid": {"row_no": "BIGINT", "invoice_id": "VARCHAR", "invoice_line": "VARCHAR", "supplier_id": "VARCHAR",
                  "po_id": "VARCHAR", "po_line": "VARCHAR", "sku": "VARCHAR", "invoice_date": "DATE",
                  "qty_billed": "BIGINT", "unit_price_minor": "BIGINT", "tax_minor": "BIGINT", "total_minor": "BIGINT",
                  "currency": "VARCHAR", "document_type": "VARCHAR", "original_invoice": "VARCHAR"},
    "inv_raw": {"row_no": "BIGINT", **{k: "VARCHAR" for k in (
        "invoice_id", "invoice_line", "supplier_id", "po_id", "po_line", "sku", "invoice_date", "qty_billed",
        "unit_price_minor", "tax_minor", "total_minor", "currency", "document_type")}},
    "inv_invalid": {"row_no": "BIGINT"},
    "po_valid": {"row_no": "BIGINT", "po_id": "VARCHAR", "po_line": "VARCHAR", "supplier_id": "VARCHAR",
                 "sku": "VARCHAR", "qty_ordered": "BIGINT", "unit_price_minor": "BIGINT", "tax_rate_bp": "BIGINT",
                 "currency": "VARCHAR", "po_date": "DATE"},
    "po_invalid": {"po_id": "VARCHAR", "po_line": "VARCHAR"},
    "gr_valid": {"row_no": "BIGINT", "receipt_id": "VARCHAR", "po_id": "VARCHAR", "po_line": "VARCHAR",
                 "sku": "VARCHAR", "qty_received": "BIGINT", "receipt_date": "DATE", "movement": "VARCHAR"},
    "issues": {"file": "VARCHAR", "row_no": "BIGINT", "field": "VARCHAR", "code": "VARCHAR", "value": "VARCHAR"},
    "suppliers": {"supplier_id": "VARCHAR"},
    "admitted": {"code": "VARCHAR"},
    "line_cap": {"currency": "VARCHAR", "cap_minor": "BIGINT"},
    "reason_map": {"code": "VARCHAR", "status": "VARCHAR", "rank": "INTEGER"},
    "status_labels": {"status": "VARCHAR", "label": "VARCHAR"},
}


def _frames(ing: Ingested, rules: RuleSet) -> dict[str, pd.DataFrame]:
    """Arma con pandas las tablas de entrada del SQL."""
    t = ing.tables
    models = lambda name: pd.DataFrame([m.model_dump() for m in t[name].valid])  # noqa: E731
    inv_raw = t["invoice_lines"].raw.reindex(columns=list(TABLE_DDL["inv_raw"]), fill_value="")
    po_raw = t["purchase_orders"].raw
    rank = {s: i for i, s in enumerate(PRECEDENCE)}
    return {
        "inv_valid": models("invoice_lines"),
        "inv_raw": inv_raw,
        "inv_invalid": pd.DataFrame({"row_no": sorted(t["invoice_lines"].invalid_rows)}),
        "po_valid": models("purchase_orders"),
        "po_invalid": po_raw[po_raw["row_no"].isin(t["purchase_orders"].invalid_rows)][["po_id", "po_line"]]
        .apply(lambda s: s.str.strip()),
        "gr_valid": models("goods_receipts"),
        "issues": ing.issues,
        "suppliers": models("suppliers") if "suppliers" in t else pd.DataFrame(),
        "admitted": pd.DataFrame({"code": list(rules.currencies)}),
        "line_cap": pd.DataFrame(list(rules.line_amount_cap_minor), columns=["currency", "cap_minor"]),
        "reason_map": pd.DataFrame([{"code": c, "status": s.value, "rank": rank[s]} for c, s in REASONS.items()]),
        "status_labels": pd.DataFrame([{"status": s.value, "label": lbl} for s, lbl in STATUS_LABEL.items()]),
    }


def reconcile(ing: Ingested, rules: RuleSet, as_of: date) -> pd.DataFrame:
    """Ejecuta reconcile.sql en DuckDB y devuelve un caso por cada fila de factura."""
    con = duckdb.connect(":memory:")
    try:
        for name, df in _frames(ing, rules).items():
            cols = TABLE_DDL[name]
            con.execute(f"CREATE TEMP TABLE {name} (" + ", ".join(f"{c} {ty}" for c, ty in cols.items()) + ")")
            if len(df):
                con.register("_src", df[list(cols)])
                con.execute(f"INSERT INTO {name} SELECT * FROM _src")
                con.unregister("_src")
        df = con.execute(SQL.read_text(encoding="utf-8"), {
            "as_of": as_of, "future_limit": as_of + timedelta(days=rules.future_days_allowed),
            "has_suppliers": "suppliers" in ing.tables, "tol_qty": rules.qty_units,
            "tol_price_bp": rules.unit_price_bp, "tol_price_abs": rules.unit_price_abs_minor,
            "tol_tax": rules.tax_abs_minor, "tol_total": rules.total_abs_minor,
        }).df()
    finally:
        con.close()
    df = df[CASE_COLUMNS]
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col]).astype("Int64")
    for col in ("po_ref", "po_supplier_id", "po_sku", "po_currency", "po_date"):
        df[col] = df[col].astype(object).where(df[col].notna(), None)
    assert len(df) == len(ing.tables["invoice_lines"].raw), "cada fila de factura debe tener exactamente un caso"
    return df


def summarize(cases: pd.DataFrame) -> dict:
    """Embudo y montos. Las categorías son disjuntas y los importes nunca se suman entre monedas."""
    counts = {s.value: int((cases["status"] == s.value).sum()) for s in Status}
    total = len(cases)
    apt = total - counts["BLOCKED"]
    linked = counts["MATCH"] + counts["EXCEPTION"]
    funnel = [
        {"etapa": "Recibidos", "lineas": total},
        {"etapa": "Aptos", "lineas": apt},
        {"etapa": "Vinculados 3-way", "lineas": linked},
        {"etapa": "Conciliados", "lineas": counts["MATCH"]},
    ]
    money = []
    comparable = cases[cases["status"].isin(["MATCH", "EXCEPTION"]) & cases["amount_expected_minor"].notna()]
    for cur, g in comparable.groupby("currency"):
        money.append({"moneda": cur, "lineas": len(g),
                      "facturado_minor": int(g["total_minor"].astype("int64").sum()),
                      "respaldado_minor": int(g["amount_expected_minor"].astype("int64").sum()),
                      "diferencia_minor": int(g["amount_diff_minor"].astype("int64").sum()),
                      "diferencia_excepciones_minor": int(
                          g.loc[g["status"] == "EXCEPTION", "amount_diff_minor"].astype("int64").sum())})
    assert sum(counts.values()) == total
    return {"lineas": total, "por_estado": counts, "embudo": funnel,
            "fuera_del_embudo": {"Bloqueados": counts["BLOCKED"], "Revisión": counts["REVIEW"],
                                 "Sin vínculo": counts["UNMATCHED"], "Discrepantes": counts["EXCEPTION"]},
            "montos_por_moneda": money}

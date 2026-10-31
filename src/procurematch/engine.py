"""Motor determinista de conciliación 3-way.

Cada línea de factura recibe exactamente un estado (MATCH, EXCEPTION, REVIEW, UNMATCHED, BLOCKED),
los códigos de motivo que lo justifican, las diferencias calculadas y la referencia exacta a la fila
de cada documento fuente. El motor nunca autoriza pagos ni registra asientos.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from . import __version__
from .ingest import Ingested, load
from .rules import DEFAULT_RULES, RuleSet, load_rules
from .schemas import STATUS_LABEL, InvoiceLine, PurchaseOrderLine, Status

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
    "PRICE_WITHIN_TOLERANCE": "Diferencia de precio dentro de tolerancia",
    "PARTIAL_RECEIPTS": "Cantidad cubierta por varias recepciones",
    "RETURN_APPLIED": "Se descontaron devoluciones de la recepción",
    "PARTIAL_BILLING": "La orden se factura en varias facturas",
}

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


def _round_bp(amount: int, bp: int) -> int:
    """amount * bp / 10000 redondeado a la unidad menor (half-up), sin flotantes."""
    n = amount * bp
    return (n + 5_000) // 10_000 if n >= 0 else -((-n + 5_000) // 10_000)


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
                          "approval_status": self.rules.approval_status, "approved_by": self.rules.approved_by},
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


def reconcile(ing: Ingested, rules: RuleSet, as_of: date) -> pd.DataFrame:
    inv_t, po_t, gr_t = ing.tables["invoice_lines"], ing.tables["purchase_orders"], ing.tables["goods_receipts"]
    suppliers = {s.supplier_id.casefold() for s in ing.tables["suppliers"].valid} if "suppliers" in ing.tables else None

    po_by_line: dict[tuple, list[PurchaseOrderLine]] = defaultdict(list)
    po_by_sku: dict[tuple, list[PurchaseOrderLine]] = defaultdict(list)
    for p in po_t.valid:
        po_by_line[(p.po_id, p.po_line)].append(p)
        po_by_sku[(p.po_id, p.sku)].append(p)
    invalid_po_keys = {(r["po_id"].strip(), r["po_line"].strip()) for r in po_t.raw.to_dict("records")
                       if r["row_no"] in po_t.invalid_rows}
    invalid_po_ids = {k[0] for k in invalid_po_keys}

    dup_receipts = set()
    if len(ing.issues):
        dup_receipts = set(ing.issues.loc[ing.issues["code"] == "DUPLICATE_RECEIPT_ID", "row_no"])
    receipts = defaultdict(list)
    for g in gr_t.valid:
        if g.receipt_date <= as_of:
            receipts[(g.po_id, g.po_line)].append(g)

    inv_keys = defaultdict(int)
    for i in inv_t.valid:
        inv_keys[(i.supplier_id.casefold(), i.invoice_id.casefold(), i.invoice_line)] += 1

    issues_by_row = defaultdict(list)
    if len(ing.issues):
        for r in ing.issues[ing.issues["file"] == inv_t.file].to_dict("records"):
            issues_by_row[r["row_no"]].append(f"{r['field']}:{r['code']}")

    cases: dict[int, dict] = {}
    for raw in inv_t.raw.to_dict("records"):  # filas inválidas: se conservan con su texto original
        if raw["row_no"] in inv_t.invalid_rows:
            c = _blank_case(raw, inv_t.file)
            c["reasons"] = sorted(issues_by_row[raw["row_no"]])
            c["status"] = Status.BLOCKED
            cases[raw["row_no"]] = c

    linked: list[tuple[InvoiceLine, PurchaseOrderLine, dict]] = []
    for inv in inv_t.valid:
        c = _blank_case(inv.model_dump(mode="json"), inv_t.file)
        r = c["reasons"]
        if inv.currency not in rules.currencies:
            r.append("CURRENCY_NOT_ADMITTED")
        if inv.invoice_date > as_of + timedelta(days=rules.future_days_allowed):
            r.append("FUTURE_DATED")
        if suppliers is not None and inv.supplier_id.casefold() not in suppliers:
            r.append("UNKNOWN_SUPPLIER")
        if inv.document_type == "INVOICE":
            if inv.qty_billed <= 0:
                r.append("NON_POSITIVE_QTY")
            if min(inv.unit_price_minor, inv.tax_minor, inv.total_minor) < 0:
                r.append("NEGATIVE_AMOUNT")
        else:
            r.append("CREDIT_NOTE")
            if not inv.original_invoice:
                r.append("CREDIT_NOTE_WITHOUT_REFERENCE")
        if inv_keys[(inv.supplier_id.casefold(), inv.invoice_id.casefold(), inv.invoice_line)] > 1:
            r.append("DUPLICATE_INVOICE")

        cands = po_by_line[(inv.po_id, inv.po_line)] if inv.po_line else po_by_sku[(inv.po_id, inv.sku)]
        po = None
        if len(cands) > 1:
            r.append("AMBIGUOUS_PO_CANDIDATES")
        elif len(cands) == 1:
            po = cands[0]
        elif (inv.po_id, inv.po_line) in invalid_po_keys or (not inv.po_line and inv.po_id in invalid_po_ids):
            r.append("PO_LINE_INVALID")
        else:
            r.append("PO_NOT_FOUND")

        if po is not None:
            _fill_po(c, po, po_t.file)
            recs = receipts[(po.po_id, po.po_line)]
            c["receipt_refs"] = ";".join(f"{gr_t.file}:fila {g.row_no}" for g in recs)
            if any(g.row_no in dup_receipts for g in recs):
                r.append("DUPLICATE_RECEIPT")
            net = sum(g.qty_received if g.movement == "RECEIPT" else -g.qty_received for g in recs)
            c["qty_received_net"] = net
            if not any(g.movement == "RECEIPT" for g in recs):
                r.append("NO_RECEIPT")
            if sum(g.movement == "RECEIPT" for g in recs) > 1:
                c["notes"].append("PARTIAL_RECEIPTS")
            if any(g.movement == "RETURN" for g in recs):
                c["notes"].append("RETURN_APPLIED")
            if inv.supplier_id.casefold() != po.supplier_id.casefold():
                r.append("SUPPLIER_MISMATCH")
            if inv.sku != po.sku:
                r.append("SKU_MISMATCH")
            if inv.currency != po.currency:
                r.append("CURRENCY_MISMATCH")
            if inv.invoice_date < po.po_date:
                r.append("INVOICE_BEFORE_PO")
            if inv.document_type == "INVOICE" and not any(REASONS[x] == Status.BLOCKED for x in r):
                linked.append((inv, po, c))  # una entrada bloqueada no se compara ni consume cantidad
        cases[inv.row_no] = c

    # Cantidades y montos: facturación acumulada por línea de orden en orden cronológico.
    billed_before: dict[tuple, int] = defaultdict(int)
    count_by_line: dict[tuple, int] = defaultdict(int)
    for inv, po, _ in linked:
        count_by_line[(po.po_id, po.po_line)] += 1
    for inv, po, c in sorted(linked, key=lambda x: (x[0].invoice_date, x[0].invoice_id, x[0].row_no)):
        key, r = (po.po_id, po.po_line), c["reasons"]
        if "DUPLICATE_INVOICE" in r:  # un duplicado no consume cantidad: lo decide el analista
            continue
        if count_by_line[key] > 1:
            c["notes"].append("PARTIAL_BILLING")
        billable = max(c["qty_received_net"] - billed_before[key], 0)
        ordered_left = max(po.qty_ordered - billed_before[key], 0)
        billed_before[key] += inv.qty_billed
        c["qty_billable"] = billable
        c["qty_diff"] = inv.qty_billed - billable
        if c["qty_diff"] > rules.qty_units and "NO_RECEIPT" not in r:
            r.append("QTY_EXCEEDS_RECEIVED")
        if inv.qty_billed - ordered_left > rules.qty_units:
            r.append("QTY_EXCEEDS_ORDERED")
        if inv.currency != po.currency:
            continue  # importes en monedas distintas no se comparan
        tol = rules.price_tolerance(po.unit_price_minor)
        c["price_tolerance_minor"] = tol
        c["price_diff_minor"] = inv.unit_price_minor - po.unit_price_minor
        if abs(c["price_diff_minor"]) > tol:
            r.append("PRICE_OUT_OF_TOLERANCE")
        elif c["price_diff_minor"]:
            c["notes"].append("PRICE_WITHIN_TOLERANCE")
        subtotal = inv.qty_billed * inv.unit_price_minor
        c["tax_expected_minor"] = _round_bp(subtotal, po.tax_rate_bp)
        c["tax_diff_minor"] = inv.tax_minor - c["tax_expected_minor"]
        if abs(c["tax_diff_minor"]) > rules.tax_abs_minor:
            r.append("TAX_MISMATCH")
        c["total_arith_diff_minor"] = inv.total_minor - (subtotal + inv.tax_minor)
        if abs(c["total_arith_diff_minor"]) > rules.total_abs_minor:
            r.append("TOTAL_ARITHMETIC_MISMATCH")
        supported = min(inv.qty_billed, billable) * po.unit_price_minor
        c["amount_expected_minor"] = supported + _round_bp(supported, po.tax_rate_bp)
        c["amount_diff_minor"] = inv.total_minor - c["amount_expected_minor"]

    rows = []
    for row_no in sorted(cases):
        c = cases[row_no]
        if c["status"] != Status.BLOCKED:
            forced = {REASONS[x] for x in c["reasons"]}
            c["status"] = next((s for s in PRECEDENCE if s in forced), Status.MATCH)
        c["status_label"] = STATUS_LABEL[c["status"]]
        c["status"] = c["status"].value
        c["reasons"] = ";".join(sorted(set(c["reasons"])))
        c["notes"] = ";".join(sorted(set(c["notes"])))
        rows.append(c)
    df = pd.DataFrame(rows, columns=CASE_COLUMNS)
    for col in NUMERIC_COLUMNS:  # en filas bloqueadas el texto inválido queda en el registro de incidencias
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    assert len(df) == len(inv_t.raw), "cada fila de factura debe tener exactamente un caso"
    return df


def _blank_case(rec: dict, file: str) -> dict:
    c = {k: None for k in CASE_COLUMNS}
    for k in ("invoice_id", "invoice_line", "supplier_id", "po_id", "po_line", "sku", "invoice_date", "currency",
              "document_type", "qty_billed", "unit_price_minor", "tax_minor", "total_minor"):
        c[k] = rec.get(k)
    c.update(row_no=rec["row_no"], case_id=f"L{rec['row_no']:05d}", invoice_ref=f"{file}:fila {rec['row_no']}",
             reasons=[], notes=[], receipt_refs="", status=None)
    return c


def _fill_po(c: dict, po: PurchaseOrderLine, file: str) -> None:
    c.update(po_ref=f"{file}:fila {po.row_no}", po_supplier_id=po.supplier_id, po_sku=po.sku,
             po_currency=po.currency, po_date=po.po_date.isoformat(), po_qty_ordered=po.qty_ordered,
             po_unit_price_minor=po.unit_price_minor, po_tax_rate_bp=po.tax_rate_bp, qty_received_net=0)


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

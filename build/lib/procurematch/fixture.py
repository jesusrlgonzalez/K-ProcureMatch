"""Fixture sintético y determinista con escenarios etiquetados.

Genera las tres fuentes (más el maestro de proveedores) y `expected_labels.csv`, que el motor
NUNCA lee: solo se usa para medir exactitud, falsas coincidencias y diferencias detectadas.
Todos los importes en unidades menores (centavos). No representa operaciones reales.
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 3, 31)
PO_COLS = ["po_id", "po_line", "supplier_id", "sku", "qty_ordered", "unit_price_minor", "tax_rate_bp",
           "currency", "po_date"]
GR_COLS = ["receipt_id", "po_id", "po_line", "sku", "qty_received", "receipt_date", "movement"]
INV_COLS = ["invoice_id", "invoice_line", "supplier_id", "po_id", "po_line", "sku", "invoice_date", "qty_billed",
            "unit_price_minor", "tax_minor", "total_minor", "currency", "document_type", "original_invoice"]
TAX_BP = {"MXN": 1600, "USD": 0}

# escenario: (estado esperado, repeticiones)
SCENARIOS = {
    "PRICE_WITHIN_TOLERANCE": ("MATCH", 6), "PARTIAL_RECEIPTS": ("MATCH", 6), "PARTIAL_BILLING": ("MATCH", 4),
    "PRICE_OUT_OF_TOLERANCE": ("EXCEPTION", 6), "QTY_OVER_RECEIPT": ("EXCEPTION", 6),
    "RETURN_NOT_CREDITED": ("EXCEPTION", 4), "TAX_MISMATCH": ("EXCEPTION", 4), "TOTAL_MISMATCH": ("EXCEPTION", 4),
    "CURRENCY_MISMATCH": ("EXCEPTION", 4), "SUPPLIER_MISMATCH": ("EXCEPTION", 4),
    "INVOICE_BEFORE_PO": ("EXCEPTION", 4),
    "DUPLICATE_INVOICE": ("REVIEW", 4), "AMBIGUOUS_PO": ("REVIEW", 4), "CREDIT_NOTE": ("REVIEW", 4),
    "UNKNOWN_PO": ("UNMATCHED", 4), "NO_RECEIPT": ("UNMATCHED", 4),
    "MISSING_INVOICE_ID": ("BLOCKED", 4), "INVALID_CURRENCY_FORMAT": ("BLOCKED", 3),
    "CURRENCY_NOT_ADMITTED": ("BLOCKED", 3), "BAD_DATE": ("BLOCKED", 4), "FUTURE_DATE": ("BLOCKED", 3),
    "NEGATIVE_QTY": ("BLOCKED", 3), "NON_NUMERIC_AMOUNT": ("BLOCKED", 3), "UNKNOWN_SUPPLIER": ("BLOCKED", 3),
    "PO_LINE_INVALID": ("BLOCKED", 3),
}


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _tax(sub: int, bp: int) -> int:
    return (sub * bp + 5_000) // 10_000


def build(out: str | Path, *, seed: int = 1905, clean_mxn: int = 520, clean_usd: int = 120) -> dict:
    rnd = random.Random(seed)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    suppliers = [{"supplier_id": f"S{j:03d}", "legal_name": f"Proveedor sintético {j:03d} SA de CV",
                  "alias": f"Prov {j:03d}"} for j in range(1, 25)]
    pos, grs, invs, truth = [], [], [], []
    seq = {"po": 0, "gr": 0, "inv": 0}

    def nxt(k: str) -> int:
        seq[k] += 1
        return seq[k]

    def new_po(cur: str = "MXN", *, line: str = "1", po_id: str | None = None, sku: str | None = None) -> dict:
        n = nxt("po")
        p = {"po_id": po_id or f"PO-{n:05d}", "po_line": line, "supplier_id": f"S{rnd.randint(1, 24):03d}",
             "sku": sku or f"SKU-{rnd.randint(1000, 9999)}", "qty_ordered": rnd.randint(4, 60),
             "unit_price_minor": rnd.randrange(20_000, 900_000, 50), "tax_rate_bp": TAX_BP[cur], "currency": cur,
             "po_date": (date(2026, 1, 2) + timedelta(days=rnd.randint(0, 40))).isoformat()}
        pos.append(p)
        return p

    def receipt(p: dict, qty: int, movement: str = "RECEIPT", delay: int = 10) -> dict:
        g = {"receipt_id": f"GR-{nxt('gr'):05d}", "po_id": p["po_id"], "po_line": p["po_line"], "sku": p["sku"],
             "qty_received": qty, "movement": movement,
             "receipt_date": (date.fromisoformat(p["po_date"]) + timedelta(days=delay)).isoformat()}
        grs.append(g)
        return g

    def invoice(p: dict, scenario: str, expected: str, *, qty: int | None = None, diff: int = 0, **kw) -> dict:
        q = p["qty_ordered"] if qty is None else qty
        unit = p["unit_price_minor"]
        sub = q * unit
        tax = _tax(sub, p["tax_rate_bp"])
        i = {"invoice_id": f"F-{nxt('inv'):06d}", "invoice_line": "1", "supplier_id": p["supplier_id"],
             "po_id": p["po_id"], "po_line": p["po_line"], "sku": p["sku"],
             "invoice_date": (date.fromisoformat(p["po_date"]) + timedelta(days=rnd.randint(14, 40))).isoformat(),
             "qty_billed": q, "unit_price_minor": unit, "tax_minor": tax, "total_minor": sub + tax,
             "currency": p["currency"], "document_type": "INVOICE", "original_invoice": ""}
        i.update(kw)
        invs.append(i)
        truth.append({"row_no": len(invs), "scenario": scenario, "expected_status": expected,
                      "injected_diff_minor": diff})
        return i

    def reprice(i: dict, unit: int) -> None:
        sub = int(i["qty_billed"]) * unit
        tax = _tax(sub, TAX_BP.get(i["currency"], 0))
        i.update(unit_price_minor=unit, tax_minor=tax, total_minor=sub + tax)

    for cur, n in (("MXN", clean_mxn), ("USD", clean_usd)):
        for _ in range(n):
            p = new_po(cur)
            receipt(p, p["qty_ordered"])
            invoice(p, f"CLEAN_{cur}", "MATCH")

    for scenario, (expected, reps) in SCENARIOS.items():
        for _ in range(reps):
            p = new_po(rnd.choice(["MXN", "MXN", "USD"]))
            q = p["qty_ordered"]
            if scenario == "NO_RECEIPT":
                invoice(p, scenario, expected)
                continue
            if scenario == "PARTIAL_RECEIPTS":
                receipt(p, q // 2, delay=8)
                receipt(p, q - q // 2, delay=12)
                invoice(p, scenario, expected)
                continue
            if scenario == "RETURN_NOT_CREDITED":
                receipt(p, q)
                receipt(p, 2, movement="RETURN", delay=15)
                unit = p["unit_price_minor"]
                invoice(p, scenario, expected, diff=2 * unit + _tax(2 * unit, p["tax_rate_bp"]))
                continue
            if scenario == "PARTIAL_BILLING":
                receipt(p, q)
                i1 = invoice(p, scenario, expected, qty=q // 2)
                i2 = invoice(p, scenario, expected, qty=q - q // 2)
                i2["invoice_date"] = max(i1["invoice_date"], i2["invoice_date"])
                continue
            if scenario == "AMBIGUOUS_PO":
                twin = new_po(p["currency"], line="2", po_id=p["po_id"], sku=p["sku"])
                receipt(p, q)
                receipt(twin, twin["qty_ordered"])
                invoice(p, scenario, expected, po_line="")
                continue
            if scenario == "PO_LINE_INVALID":
                receipt(p, q)
                invoice(p, scenario, expected)
                p["po_date"] = "2026-13-40"  # la orden referida es inválida
                continue
            receipt(p, q)
            if scenario == "DUPLICATE_INVOICE":
                first = invoice(p, scenario, expected)
                invs.append(dict(first))
                truth.append({"row_no": len(invs), "scenario": scenario, "expected_status": expected,
                              "injected_diff_minor": 0})
                continue
            i = invoice(p, scenario, expected)
            unit = p["unit_price_minor"]
            if scenario == "PRICE_WITHIN_TOLERANCE":
                reprice(i, unit + rules_tolerance(unit) // 2)
            elif scenario == "PRICE_OUT_OF_TOLERANCE":
                bump = max(unit * 5 // 100, 500)
                reprice(i, unit + bump)
                truth[-1]["injected_diff_minor"] = int(i["total_minor"]) - (q * unit + _tax(q * unit, p["tax_rate_bp"]))
            elif scenario == "QTY_OVER_RECEIPT":
                i["qty_billed"] = q + 3
                reprice(i, unit)
                truth[-1]["injected_diff_minor"] = int(i["total_minor"]) - (q * unit + _tax(q * unit, p["tax_rate_bp"]))
            elif scenario == "TAX_MISMATCH":
                i["tax_minor"] = int(i["tax_minor"]) + 500
                i["total_minor"] = int(i["total_minor"]) + 500
                truth[-1]["injected_diff_minor"] = 500
            elif scenario == "TOTAL_MISMATCH":
                i["total_minor"] = int(i["total_minor"]) + 320
                truth[-1]["injected_diff_minor"] = 320
            elif scenario == "CURRENCY_MISMATCH":
                i["currency"] = "USD" if p["currency"] == "MXN" else "MXN"
            elif scenario == "SUPPLIER_MISMATCH":
                i["supplier_id"] = "S024" if p["supplier_id"] != "S024" else "S023"
            elif scenario == "INVOICE_BEFORE_PO":
                i["invoice_date"] = (date.fromisoformat(p["po_date"]) - timedelta(days=5)).isoformat()
            elif scenario == "CREDIT_NOTE":
                i.update(document_type="CREDIT_NOTE", qty_billed=-1, original_invoice="")
            elif scenario == "UNKNOWN_PO":
                i["po_id"] = "PO-INEXISTENTE"
            elif scenario == "MISSING_INVOICE_ID":
                i["invoice_id"] = ""
            elif scenario == "INVALID_CURRENCY_FORMAT":
                i["currency"] = "pesos"
            elif scenario == "CURRENCY_NOT_ADMITTED":
                i["currency"] = "EUR"
            elif scenario == "BAD_DATE":
                i["invoice_date"] = "2026-02-31"
            elif scenario == "FUTURE_DATE":
                i["invoice_date"] = (AS_OF + timedelta(days=45)).isoformat()
            elif scenario == "NEGATIVE_QTY":
                i["qty_billed"] = -5
            elif scenario == "NON_NUMERIC_AMOUNT":
                i["total_minor"] = "12,500.00"
            elif scenario == "UNKNOWN_SUPPLIER":
                i["supplier_id"] = "S999"

    _write(out / "suppliers.csv", ["supplier_id", "legal_name", "alias"], suppliers)
    _write(out / "purchase_orders.csv", PO_COLS, pos)
    _write(out / "goods_receipts.csv", GR_COLS, grs)
    _write(out / "invoice_lines.csv", INV_COLS, invs)
    _write(out / "expected_labels.csv", ["row_no", "scenario", "expected_status", "injected_diff_minor"], truth)
    return {"suppliers": len(suppliers), "po_lines": len(pos), "receipts": len(grs), "invoice_lines": len(invs),
            "seed": seed, "as_of": AS_OF.isoformat()}


def rules_tolerance(unit: int) -> int:
    """Tolerancia de precio de las reglas R1 (0.5 % o 100), replicada para fabricar casos límite."""
    return max(100, unit * 50 // 10_000)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/sample")
    print(build(ap.parse_args().out))

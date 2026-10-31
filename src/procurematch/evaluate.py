"""Indicadores de validación frente a etiquetas del fixture y frente al motor anterior (v0).

- Exactitud de clasificación (5 estados).
- Falsa coincidencia: líneas declaradas MATCH cuyo estado correcto no es MATCH (el error más caro).
- Diferencia monetaria: importe inyectado vs importe detectado en las discrepancias.
- Esfuerzo de revisión: proporción de líneas que exigen a un analista, frente a
  (a) revisión manual de todas las líneas y (b) el motor de reglas v0 (`src/i19`).
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

from .engine import RunResult, run
from .schemas import Status


def classification(cases: pd.DataFrame, labels: pd.DataFrame) -> dict:
    m = cases[["row_no", "status", "currency", "amount_diff_minor"]].merge(labels, on="row_no", how="inner",
                                                                            validate="1:1")
    if len(m) != len(cases):
        raise ValueError("las etiquetas no cubren todas las líneas")
    pred_match, true_match = m["status"].eq("MATCH"), m["expected_status"].eq("MATCH")
    per_status = {}
    for s in Status:
        tp = int((m["status"].eq(s.value) & m["expected_status"].eq(s.value)).sum())
        pp, ap = int(m["status"].eq(s.value).sum()), int(m["expected_status"].eq(s.value).sum())
        per_status[s.value] = {"precision": round(tp / pp, 4) if pp else None,
                               "recall": round(tp / ap, 4) if ap else None, "support": ap}
    injected = m[m["injected_diff_minor"] != 0].assign(
        detected=lambda x: x["amount_diff_minor"].fillna(0).astype("int64"))
    money = [{"moneda": cur, "casos": len(g), "inyectada_minor": int(g["injected_diff_minor"].sum()),
              "detectada_minor": int(g["detected"].sum()),
              "casos_detectados_exactos": int((g["detected"] == g["injected_diff_minor"]).sum())}
             for cur, g in injected.groupby("currency")]
    return {
        "lineas": len(m),
        "exactitud": round(float(m["status"].eq(m["expected_status"]).mean()), 4),
        "falsas_coincidencias": int((pred_match & ~true_match).sum()),
        "tasa_falsa_coincidencia": round(float((pred_match & ~true_match).sum() / max(pred_match.sum(), 1)), 4),
        "coincidencias_no_detectadas": int((~pred_match & true_match).sum()),
        "por_estado": per_status,
        "confusion": pd.crosstab(m["expected_status"], m["status"]).to_dict(),
        "diferencia_monetaria_por_moneda": money,   # nunca se suman monedas distintas
        "esfuerzo_revision": round(float((~pred_match).mean()), 4),
    }


def legacy_v0(source_dir: Path, labels: pd.DataFrame) -> dict:
    """Ejecuta el motor v0 sobre las mismas líneas (adaptando nombres de columnas) y lo mide igual."""
    from i19.reconcile import reconcile as v0  # motor heredado, sin modificar

    src = {n: list(csv.DictReader((source_dir / f).open(encoding="utf-8")))
           for n, f in [("s", "suppliers.csv"), ("p", "purchase_orders.csv"), ("g", "goods_receipts.csv"),
                        ("i", "invoice_lines.csv")]}
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        _w(t / "vendors.csv", [{"supplier_id": s["supplier_id"], "alias": s["alias"]} for s in src["s"]])
        _w(t / "purchase_orders.csv", [{**p, "unit_price_cents": p["unit_price_minor"]} for p in src["p"]])
        _w(t / "goods_receipts.csv", [{**g, "qty_received": g["qty_received"] if g["movement"] != "RETURN"
                                       else f"-{g['qty_received']}"} for g in src["g"]])
        _w(t / "invoice_lines_dirty.csv", [{**i, "supplier_ref": i["supplier_id"],
                                            "unit_price_cents": i["unit_price_minor"], "tax_cents": i["tax_minor"],
                                            "total_cents": i["total_minor"]} for i in src["i"]])
        _, logs = v0(t)
    pred = pd.DataFrame(logs)[["row_no", "status"]].merge(labels, on="row_no")
    pm, tm = pred["status"].eq("MATCH_CANDIDATE"), pred["expected_status"].eq("MATCH")
    return {"falsas_coincidencias": int((pm & ~tm).sum()),
            "coincidencias_no_detectadas": int((~pm & tm).sum()),
            "exactitud_binaria_match": round(float((pm == tm).mean()), 4),
            "esfuerzo_revision": round(float((~pm).mean()), 4),
            "limitaciones": "solo MXN, IVA fijo 16 %, sin tolerancias, sin devoluciones ni facturación parcial"}


def _w(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def evaluate(result: RunResult, source_dir: Path) -> dict:
    labels = pd.read_csv(source_dir / "expected_labels.csv")
    current = classification(result.cases, labels)
    v0 = legacy_v0(source_dir, labels)
    return {"run_id": result.run_id, "rules_version": result.rules.version, "as_of": result.as_of.isoformat(),
            "datos": "sintéticos (fixture etiquetado)", "motor_actual": current, "motor_v0": v0,
            "revision_manual_total": {"esfuerzo_revision": 1.0,
                                      "supuesto": "proceso base: un analista coteja cada línea a mano"},
            "decision_ml": "No se incorpora ML: las reglas deterministas ya alcanzan "
                           f"{current['falsas_coincidencias']} falsas coincidencias en este fixture; un modelo "
                           "solo se evaluará con datos reales y si reduce el esfuerzo sin aumentar falsas coincidencias."}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="data/sample")
    ap.add_argument("--as-of", default="2026-03-31")
    ap.add_argument("--out", default="reports/metrics.json")
    a = ap.parse_args()
    src = Path(a.source)
    res = run(src, as_of=date.fromisoformat(a.as_of))
    rep = evaluate(res, src)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("motor_actual", "motor_v0")}, ensure_ascii=False, indent=1, default=str)[:3000])


if __name__ == "__main__":
    main()

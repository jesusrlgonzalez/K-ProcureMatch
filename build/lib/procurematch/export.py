"""Expediente de evidencias: casos con decisión humana, motivos, referencias y hashes.

Formatos: CSV, JSON y HTML imprimible. El expediente no contiene ni ejecuta instrucciones de pago,
asientos contables ni órdenes de ERP.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import zipfile
from datetime import datetime, timezone

import pandas as pd

from .engine import REASON_TEXT
from .views import kpis

EXPORT_COLUMNS = ["case_id", "invoice_ref", "invoice_id", "invoice_line", "supplier_id", "po_id", "po_line", "sku",
                  "invoice_date", "currency", "document_type", "status", "status_label", "reasons", "notes",
                  "qty_billed", "qty_billable", "qty_diff", "unit_price_minor", "po_unit_price_minor",
                  "price_diff_minor", "tax_minor", "tax_expected_minor", "tax_diff_minor", "total_minor",
                  "amount_expected_minor", "amount_diff_minor", "po_ref", "receipt_refs",
                  "review_state", "decision", "analyst", "decision_comment", "resolved_at"]


def evidence_zip(manifest: dict, cases: pd.DataFrame, issues: pd.DataFrame, coverage: pd.DataFrame,
                 filters: dict | None = None) -> bytes:
    for col in EXPORT_COLUMNS:  # una corrida sin decisiones aún exporta las columnas de decisión vacías
        if col not in cases:
            cases = cases.assign(**{col: "PENDIENTE" if col == "review_state" else None})
    rows = cases[EXPORT_COLUMNS]
    summary = kpis(cases)
    files = {
        "casos.csv": rows.to_csv(index=False).encode("utf-8"),
        "incidencias_validacion.csv": issues.to_csv(index=False).encode("utf-8"),
        "cobertura.csv": coverage.to_csv(index=False).encode("utf-8"),
        "resumen.json": json.dumps(summary, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
    }
    files["expediente.html"] = _html(manifest, rows, summary, filters).encode("utf-8")
    full = dict(manifest)
    full.update(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), filters=filters or {},
                exported_cases=len(rows),
                decisions={"resueltos": int((rows["review_state"] == "RESUELTO").sum()),
                           "pendientes": int((rows["review_state"] != "RESUELTO").sum())},
                files_sha256={n: hashlib.sha256(b).hexdigest() for n, b in files.items()})
    files["manifest.json"] = json.dumps(full, ensure_ascii=False, indent=2).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(f"expediente_{manifest['run_id']}/{name}", data)
    return buf.getvalue()


def _money(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{int(v) / 100:,.2f}"


def _html(manifest: dict, rows: pd.DataFrame, summary: dict, filters: dict | None) -> str:
    e = html.escape
    src = "".join(f"<tr><td>{e(f)}</td><td>{m['rows']}</td><td><code>{m['sha256']}</code></td></tr>"
                  for f, m in manifest["sources"].items())
    funnel = "".join(f"<tr><td>{e(x['etapa'])}</td><td>{x['lineas']}</td></tr>" for x in summary["embudo"])
    side = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in summary["fuera_del_embudo"].items())
    money = "".join(f"<tr><td>{m['moneda']}</td><td>{m['lineas']}</td><td>{_money(m['facturado_minor'])}</td>"
                    f"<td>{_money(m['respaldado_minor'])}</td><td>{_money(m['diferencia_minor'])}</td></tr>"
                    for m in summary["montos_por_moneda"])
    body = []
    for r in rows.to_dict("records"):
        why = "<br>".join(e(f"{c}: {REASON_TEXT.get(c.split(':')[-1], c)}") for c in str(r["reasons"] or "").split(";") if c)
        body.append(
            f"<tr class='{e(r['status'])}'><td>{e(r['case_id'])}</td><td>{e(str(r['invoice_id'] or ''))}</td>"
            f"<td>{e(str(r['supplier_id'] or ''))}</td><td>{e(str(r['currency'] or ''))}</td>"
            f"<td>{e(r['status_label'])}</td><td>{why}</td><td class='n'>{_money(r['total_minor'])}</td>"
            f"<td class='n'>{_money(r['amount_diff_minor'])}</td>"
            f"<td>{e(r['invoice_ref'])}<br>{e(str(r['po_ref'] or ''))}<br>{e(str(r['receipt_refs'] or ''))}</td>"
            f"<td>{e(str(r['decision'] or 'PENDIENTE'))}<br><small>{e(str(r['analyst'] or ''))}</small></td></tr>")
    rules = manifest["rules"]
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Expediente {e(manifest['run_id'])}</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#1f2933}}table{{border-collapse:collapse;margin:8px 0 20px;font-size:12px}}
td,th{{border:1px solid #cbd2d9;padding:4px 6px;vertical-align:top}}th{{background:#f0f4f8}}.n{{text-align:right}}
.EXCEPTION{{background:#fff4e5}}.BLOCKED{{background:#fdecea}}.REVIEW{{background:#eef2ff}}.UNMATCHED{{background:#f5f5f5}}
code{{font-size:10px}}@media print{{body{{margin:0}}}}</style></head><body>
<h1>Expediente de conciliación 3-way</h1>
<p><b>Corrida:</b> {e(manifest['run_id'])} · <b>Fecha de corte:</b> {e(manifest['as_of'])} ·
<b>Motor:</b> {e(manifest['engine_version'])} · <b>Reglas:</b> {e(rules['version'])} (<code>{rules['sha256']}</code>,
aprobación: {e(str(rules['approval_status']))})</p>
<p><b>Filtros aplicados:</b> {e(json.dumps(filters or {}, ensure_ascii=False))}</p>
<p><b>Alcance de autoridad:</b> este expediente es soporte para revisión. No ejecuta pagos, asientos contables
ni órdenes en ERP.</p>
<h2>Fuentes</h2><table><tr><th>Archivo</th><th>Filas</th><th>SHA-256</th></tr>{src}</table>
<h2>Embudo</h2><table><tr><th>Etapa</th><th>Líneas</th></tr>{funnel}</table>
<table><tr><th>Fuera del embudo / discrepantes</th><th>Líneas</th></tr>{side}</table>
<h2>Importes por moneda (no se suman entre monedas)</h2>
<table><tr><th>Moneda</th><th>Líneas vinculadas</th><th>Facturado</th><th>Respaldado por OC+recepción</th><th>Diferencia</th></tr>{money}</table>
<h2>Casos ({len(rows)})</h2>
<table><tr><th>Caso</th><th>Factura</th><th>Proveedor</th><th>Moneda</th><th>Estado</th><th>Motivos</th><th>Total</th>
<th>Diferencia</th><th>Fuentes</th><th>Decisión</th></tr>{''.join(body)}</table></body></html>"""

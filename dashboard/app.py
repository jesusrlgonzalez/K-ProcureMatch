"""Dashboard de conciliación 3-way para el analista de cuentas por pagar.

Ejecutar:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from procurematch.engine import REASON_TEXT  # noqa: E402
from procurematch.ingest import TABLES, SchemaError  # noqa: E402
from procurematch.rules import load_rules  # noqa: E402
from procurematch.schemas import STATUS_LABEL, Decision, Resolution, Status  # noqa: E402
from procurematch.service import Service  # noqa: E402
from procurematch.views import Filters, apply, kpis, reason_catalog  # noqa: E402

SAMPLE = ROOT / "data" / "sample"
DB = os.environ.get("KPM_DB", str(ROOT / "data" / "kpm_dashboard.duckdb"))
COLORS = {"MATCH": "#2e7d32", "EXCEPTION": "#ef6c00", "REVIEW": "#3949ab", "UNMATCHED": "#757575", "BLOCKED": "#c62828"}

st.set_page_config(page_title="K-ProcureMatch", page_icon="🧾", layout="wide")


def money(minor, cur: str = "") -> str:
    if minor is None or pd.isna(minor):
        return "—"
    return f"{int(minor) / 100:,.2f} {cur}".strip()


MONEY_FIELDS = {"Precio unitario", "Impuesto", "Total"}


def cell(v, campo: str, cur: str) -> str:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, str):
        return v
    return money(v, cur) if campo in MONEY_FIELDS else f"{int(v):,}"


@st.cache_resource
def demo_service() -> Service:
    return Service(SAMPLE, DB)


# ---------------------------------------------------------------- fuente y corrida
st.sidebar.title("K-ProcureMatch")
rules = load_rules()
st.sidebar.caption(f"Reglas **{rules.version}** · aprobación: `{rules.approval_status}`  \n"
                   f"Tolerancia precio: {rules.unit_price_bp / 100:.2f} % o {rules.unit_price_abs_minor / 100:.2f}")
mode = st.sidebar.radio("Fuente de datos", ["Datos de demostración", "Subir mis archivos"], key="mode")

svc: Service | None = None
if mode == "Datos de demostración":
    svc = demo_service()
    default_as_of = date(2026, 3, 31)
else:
    default_as_of = date.today()
    st.sidebar.caption("Órdenes, recepciones y facturas son obligatorias; el maestro de proveedores es opcional.")
    ups = {name: st.sidebar.file_uploader(fname, type="csv", key=f"up_{name}") for name, (fname, _, _) in TABLES.items()}
    if all(ups[n] for n in ("purchase_orders", "goods_receipts", "invoice_lines")):
        key = tuple((n, u.file_id) for n, u in ups.items() if u)
        if st.session_state.get("upload_key") != key:
            tmp = Path(tempfile.mkdtemp(prefix="kpm_"))
            for n, u in ups.items():
                if u:
                    (tmp / TABLES[n][0]).write_bytes(u.getvalue())
            st.session_state.update(upload_key=key, upload_svc=Service(tmp, DB), run_id=None)
        svc = st.session_state["upload_svc"]

as_of = st.sidebar.date_input("Fecha de corte", default_as_of, key="as_of")
if svc is None:
    st.info("Suba las tres tablas para comenzar.")
    st.stop()

try:
    if st.sidebar.button("Ejecutar conciliación", type="primary") or st.session_state.get("run_svc") is not svc \
            or st.session_state.get("run_as_of") != as_of:
        res = svc.new_run(as_of)
        st.session_state.update(run_id=res.run_id, run_svc=svc, run_as_of=as_of)
except SchemaError as e:
    st.error(f"Esquema inválido: {e}")
    st.stop()

run_id = st.session_state["run_id"]
res = svc.result(run_id)
all_cases = svc.cases(run_id)
st.sidebar.caption(f"Corrida `{run_id}` · corte {res.as_of.isoformat()}")

# ---------------------------------------------------------------- filtros
st.sidebar.header("Filtros")
dates = pd.to_datetime(all_cases["invoice_date"], errors="coerce", format="%Y-%m-%d").dropna()
f = Filters(
    suppliers=st.sidebar.multiselect("Proveedor", sorted(all_cases["supplier_id"].dropna().unique()), key="f_supplier", placeholder="Todos"),
    currencies=st.sidebar.multiselect("Moneda", sorted(all_cases["currency"].dropna().unique()), key="f_currency", placeholder="Todas"),
    statuses=st.sidebar.multiselect("Estado", [s.value for s in Status], format_func=lambda s: STATUS_LABEL[Status(s)],
                                    key="f_status", placeholder="Todos"),
    reasons=st.sidebar.multiselect("Discrepancia / motivo", reason_catalog(all_cases), key="f_reason",
                             placeholder="Todos"),
    review_states=st.sidebar.multiselect("Revisión", ["PENDIENTE", "RESUELTO"], key="f_review", placeholder="Todos"),
)
if not dates.empty:
    full = (dates.min().date(), dates.max().date())
    period = st.sidebar.date_input("Periodo de factura", full, key="f_period")
    if isinstance(period, tuple) and len(period) == 2 and tuple(period) != full:
        f.date_from, f.date_to = period
        f.include_undated = st.sidebar.checkbox("Incluir facturas sin fecha válida", True, key="f_undated")
cases = apply(all_cases, f)
k = kpis(cases)

# ---------------------------------------------------------------- vista
st.title("Conciliación 3-way · órdenes, recepciones y facturas")
st.caption("Soporte a decisión del analista: el sistema no aprueba pagos ni registra asientos.")
tab_res, tab_cov, tab_cases, tab_exp = st.tabs(["Resumen", "Cobertura de datos", "Casos", "Expediente"])

with tab_res:
    c = st.columns(5)
    c[0].metric("Líneas", k["lineas"])
    c[1].metric("Conciliadas", k["por_estado"]["MATCH"], f"{k['tasa_conciliacion']:.1%}", delta_color="off")
    c[2].metric("Discrepantes", k["por_estado"]["EXCEPTION"])
    c[3].metric("Requieren analista", k["requieren_analista"], f"{k['esfuerzo_revision']:.1%} de las líneas",
                delta_color="off")
    c[4].metric("Pendientes de decisión", k["pendientes_de_decision"])

    st.subheader("Indicadores financieros por moneda")
    st.caption("Solo líneas con vínculo 3-way único. Los importes nunca se suman entre monedas.")
    if k["montos_por_moneda"]:
        cols = st.columns(len(k["montos_por_moneda"]))
        for col, m in zip(cols, k["montos_por_moneda"]):
            col.markdown(f"**{m['moneda']}** · {m['lineas']} líneas")
            col.metric("Facturado", money(m["facturado_minor"], m["moneda"]))
            col.metric("Respaldado por OC + recepción", money(m["respaldado_minor"], m["moneda"]))
            col.metric("Diferencia en discrepantes", money(m["diferencia_excepciones_minor"], m["moneda"]))
    else:
        st.info("Sin líneas vinculadas en la selección.")

    left, right = st.columns(2)
    fun = k["embudo"]
    fig = go.Figure(go.Funnel(y=[x["etapa"] for x in fun], x=[x["lineas"] for x in fun], textinfo="value+percent initial",
                              marker={"color": ["#90a4ae", "#78909c", "#546e7a", COLORS["MATCH"]]}))
    fig.update_layout(title="Embudo de conciliación", margin=dict(l=10, r=10, t=40, b=10), height=330)
    left.plotly_chart(fig, width="stretch")
    side = k["por_estado"]
    fig2 = go.Figure(go.Bar(x=[STATUS_LABEL[Status(s)] for s in side], y=list(side.values()),
                            marker_color=[COLORS[s] for s in side], text=list(side.values()), textposition="outside"))
    fig2.update_layout(title="Líneas por estado (categorías disjuntas)", margin=dict(l=10, r=10, t=40, b=10), height=330)
    right.plotly_chart(fig2, width="stretch")

    codes = cases["reasons"].fillna("").str.split(";").explode()
    codes = codes[codes != ""].value_counts()
    if not codes.empty:
        fig3 = go.Figure(go.Bar(y=[REASON_TEXT.get(x.split(":")[-1], x) for x in codes.index], x=codes.values,
                                orientation="h", marker_color="#546e7a"))
        fig3.update_layout(title="Motivos de discrepancia", height=max(260, 26 * len(codes)),
                           margin=dict(l=10, r=10, t=40, b=10), yaxis={"autorange": "reversed"})
        st.plotly_chart(fig3, width="stretch")
    ok = sum(k["por_estado"].values()) == len(cases)
    st.caption(("✅" if ok else "❌") + f" Control de totales: {sum(k['por_estado'].values())} líneas clasificadas = "
               f"{len(cases)} líneas filtradas.")

with tab_cov:
    st.subheader("Cobertura, periodos y registros inválidos")
    st.dataframe(res.ingested.coverage, hide_index=True, width="stretch")
    st.subheader(f"Incidencias de validación ({len(res.ingested.issues)})")
    st.caption("Ninguna fila se elimina: cada incidencia se identifica por archivo y fila.")
    st.dataframe(res.ingested.issues, hide_index=True, width="stretch")

with tab_cases:
    st.subheader(f"Casos ({len(cases)})")
    view = cases[["case_id", "status_label", "supplier_id", "invoice_id", "po_id", "invoice_date", "currency",
                  "total_minor", "amount_diff_minor", "reasons", "review_state", "decision"]].copy()
    view["total_minor"] = view["total_minor"].map(money)
    view["amount_diff_minor"] = view["amount_diff_minor"].map(money)
    view["decision"] = view["decision"].fillna("")
    view.columns = ["Caso", "Estado", "Proveedor", "Factura", "Orden", "Fecha", "Moneda", "Total", "Diferencia",
                    "Motivos", "Revisión", "Decisión"]
    st.dataframe(view, hide_index=True, width="stretch", height=280)
    if not cases.empty:
        cid = st.selectbox("Abrir caso", cases["case_id"], key="case_pick",
                           format_func=lambda x: f"{x} · {cases.set_index('case_id').loc[x, 'status_label']}")
        d = svc.case_detail(run_id, cid)
        cc = d["caso"]
        st.markdown(f"### {cid} · {cc['status_label']}  \nFuente: `{cc['invoice_ref']}`")
        for m in d["motivos"]:
            st.warning(f"**{m['codigo']}** — {m['descripcion']}")
        for n in d["notas"]:
            st.caption(f"ℹ️ {n['codigo']} — {n['descripcion']}")
        a, b, c3 = st.columns(3)
        a.markdown("**Orden de compra**")
        a.json(d["documentos"]["orden"][0] if d["documentos"]["orden"] else {"vínculo": "sin orden única"})
        b.markdown(f"**Recepciones ({len(d['documentos']['recepciones'])})**")
        b.json(d["documentos"]["recepciones"] or {"vínculo": "sin recepción"})
        c3.markdown("**Factura**")
        c3.json(d["documentos"]["factura"][0])
        comp = pd.DataFrame(d["comparacion"])
        cur = cc.get("currency") or ""
        for col in ("orden", "recepcion", "factura", "esperado", "diferencia", "tolerancia"):
            comp[col] = [cell(v, campo, cur) for v, campo in zip(comp[col], comp["campo"])]
        comp.columns = ["Campo", "Orden de compra", "Recepción", "Factura", "Esperado", "Diferencia", "Tolerancia"]
        st.markdown("**Comparador de documentos**")
        st.dataframe(comp, hide_index=True, width="stretch")

        st.markdown("**Resolución del analista**")
        if cc.get("decision"):
            st.success(f"Última decisión: {cc['decision']} · {cc['analyst']} · {cc['resolved_at']}")
        with st.form(f"res_{cid}"):
            dec = st.selectbox("Decisión", [x.value for x in Decision])
            who = st.text_input("Analista")
            why = st.text_area("Justificación")
            if st.form_submit_button("Registrar decisión"):
                try:
                    svc.resolve(run_id, cid, Resolution(decision=dec, analyst=who, comment=why))
                    st.rerun()
                except ValueError as e:
                    st.error(f"Decisión no registrada: {e}")

with tab_exp:
    st.subheader("Exportar expediente")
    st.write(f"Incluye {len(cases)} casos de la vista filtrada con decisión humana, motivos, referencias a las filas "
             "fuente, hash SHA-256 de cada archivo de entrada y versión de reglas.")
    st.caption("Formatos: CSV, JSON y HTML imprimible (para PDF, imprimir el HTML).")
    st.download_button("Descargar expediente (.zip)", svc.export(run_id, f), file_name=f"expediente_{run_id}.zip",
                       mime="application/zip", type="primary")

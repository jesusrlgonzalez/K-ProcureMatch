"""Dashboard de conciliación 3-way para el analista de cuentas por pagar.

Es un cliente de la API: todos los datos (corridas, cobertura, filtros, casos, decisiones y expedientes)
llegan por HTTP. No importa el motor ni la base de datos.

Ejecutar:  python run.py dashboard      (levanta la API y el dashboard)
           KPM_API_URL=http://host:8000 streamlit run dashboard/app.py   (API ya en marcha)
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from procurematch.client import REQUIRED_SOURCES, SOURCE_FILES, ApiClient, ApiError  # noqa: E402
from procurematch.filters import Filters  # noqa: E402
from procurematch.schemas import STATUS_LABEL, Decision, Status  # noqa: E402

API_URL = os.environ.get("KPM_API_URL", "http://127.0.0.1:8000")
DEMO_AS_OF = date(2026, 3, 31)
COLORS = {"MATCH": "#2e7d32", "EXCEPTION": "#ef6c00", "REVIEW": "#3949ab", "UNMATCHED": "#757575", "BLOCKED": "#c62828"}
MONEY_FIELDS = {"Precio unitario", "Impuesto", "Total"}

st.set_page_config(page_title="K-ProcureMatch", page_icon="🧾", layout="wide")


def money(minor, cur: str = "") -> str:
    if minor is None or pd.isna(minor):
        return "—"
    return f"{int(minor) / 100:,.2f} {cur}".strip()


def cell(v, campo: str, cur: str) -> str:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, str):
        return v
    return money(v, cur) if campo in MONEY_FIELDS else f"{int(v):,}"


@st.cache_resource
def api(url: str) -> ApiClient:
    return ApiClient(url)


@st.cache_data(show_spinner=False)
def facets(url: str, run_id: str) -> dict:          # inmutable por corrida
    return api(url).facets(run_id)


@st.cache_data(show_spinner=False)
def data_quality(url: str, run_id: str):            # inmutable por corrida
    return api(url).data_quality(run_id)


def fail(e: ApiError) -> None:
    if e.status == 0:
        st.error(f"No hay conexión con la API en `{API_URL}`. Inicie el sistema con `python run.py dashboard` "
                 "o defina `KPM_API_URL`.")
    else:
        st.error(f"La API respondió {e.status}: {e.detail}")
    st.stop()


client = api(API_URL)
try:
    rules = client.rules()
except ApiError as e:
    fail(e)

# ---------------------------------------------------------------- fuente y corrida
st.sidebar.title("K-ProcureMatch")
tol = rules["tolerancias"]
st.sidebar.caption(f"Reglas **{rules['version']}** · aprobación: `{rules['aprobacion']['estado']}`  \n"
                   f"Tolerancia precio: {tol['precio_bp'] / 100:.2f} % o {tol['precio_abs_minor'] / 100:.2f}  \n"
                   f"API: `{API_URL}`")
mode = st.sidebar.radio("Fuente de datos", ["Datos de demostración", "Subir mis archivos"], key="mode")

dataset_id: str | None = None
if mode == "Datos de demostración":
    dataset_id, default_as_of = "demo", DEMO_AS_OF
else:
    default_as_of = date.today()
    st.sidebar.caption("Órdenes, recepciones y facturas son obligatorias; el maestro de proveedores es opcional.")
    ups = {name: st.sidebar.file_uploader(fname, type="csv", key=f"up_{name}") for name, fname in SOURCE_FILES.items()}
    if all(ups[n] for n in REQUIRED_SOURCES):
        key = tuple((n, u.file_id) for n, u in ups.items() if u)
        if st.session_state.get("upload_key") != key:
            try:
                up = client.upload({n: u.getvalue() for n, u in ups.items() if u})
            except ApiError as e:
                fail(e)
            st.session_state.update(upload_key=key, upload_dataset=up["dataset_id"])
        dataset_id = st.session_state["upload_dataset"]

as_of = st.sidebar.date_input("Fecha de corte", default_as_of, key="as_of")
if dataset_id is None:
    st.info("Suba las tres tablas para comenzar.")
    st.stop()

try:
    if st.sidebar.button("Ejecutar conciliación", type="primary") \
            or st.session_state.get("run_key") != (dataset_id, as_of):
        r = client.new_run(as_of, dataset_id)
        st.session_state.update(run_id=r["run_id"], run_key=(dataset_id, as_of), manifest=r["manifest"])
    run_id, manifest = st.session_state["run_id"], st.session_state["manifest"]
    fac = facets(API_URL, run_id)
except ApiError as e:
    fail(e)
st.sidebar.caption(f"Corrida `{run_id}` · corte {manifest['as_of']} · reglas {manifest['rules']['version']}")

# ---------------------------------------------------------------- filtros (valores ofrecidos por la API)
st.sidebar.header("Filtros")
f = Filters(
    suppliers=st.sidebar.multiselect("Proveedor", fac["proveedores"], key="f_supplier", placeholder="Todos"),
    currencies=st.sidebar.multiselect("Moneda", fac["monedas"], key="f_currency", placeholder="Todas"),
    statuses=st.sidebar.multiselect("Estado", [s.value for s in Status], format_func=lambda s: STATUS_LABEL[Status(s)],
                                    key="f_status", placeholder="Todos"),
    reasons=st.sidebar.multiselect("Discrepancia / motivo", [m["codigo"] for m in fac["motivos"]], key="f_reason",
                                   placeholder="Todos"),
    review_states=st.sidebar.multiselect("Revisión", fac["revision"], key="f_review", placeholder="Todos"),
)
if fac["fecha_min"]:
    full = (date.fromisoformat(fac["fecha_min"]), date.fromisoformat(fac["fecha_max"]))
    period = st.sidebar.date_input("Periodo de factura", full, key="f_period")
    if isinstance(period, tuple) and len(period) == 2 and tuple(period) != full:
        f.date_from, f.date_to = period
        f.include_undated = st.sidebar.checkbox("Incluir facturas sin fecha válida", True, key="f_undated")

try:
    k = client.summary(run_id, f)
    cases = client.cases(run_id, f)
except ApiError as e:
    fail(e)

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

    if k["motivos"]:
        mot = k["motivos"]
        fig3 = go.Figure(go.Bar(y=[m["descripcion"] for m in mot], x=[m["lineas"] for m in mot], orientation="h",
                                marker_color="#546e7a"))
        fig3.update_layout(title="Motivos de discrepancia", height=max(260, 26 * len(mot)),
                           margin=dict(l=10, r=10, t=40, b=10), yaxis={"autorange": "reversed"})
        st.plotly_chart(fig3, width="stretch")
    classified = sum(k["por_estado"].values())
    ok = classified == k["lineas"] == len(cases)
    st.caption(("✅" if ok else "❌") + f" Control de totales: {classified} líneas clasificadas (resumen) = "
               f"{len(cases)} casos recibidos (detalle).")

with tab_cov:
    try:
        coverage, issues = data_quality(API_URL, run_id)
    except ApiError as e:
        fail(e)
    st.subheader("Cobertura, periodos y registros inválidos")
    st.dataframe(coverage, hide_index=True, width="stretch")
    st.subheader(f"Incidencias de validación ({len(issues)})")
    st.caption("Ninguna fila se elimina: cada incidencia se identifica por archivo y fila.")
    st.dataframe(issues, hide_index=True, width="stretch")

with tab_cases:
    st.subheader(f"Casos ({len(cases)})")
    if cases.empty:
        st.info("Ningún caso coincide con los filtros.")
    else:
        view = cases[["case_id", "status_label", "supplier_id", "invoice_id", "po_id", "invoice_date", "currency",
                      "total_minor", "amount_diff_minor", "reasons", "review_state", "decision"]].copy()
        view["total_minor"] = view["total_minor"].map(money)
        view["amount_diff_minor"] = view["amount_diff_minor"].map(money)
        view["decision"] = view["decision"].fillna("")
        view.columns = ["Caso", "Estado", "Proveedor", "Factura", "Orden", "Fecha", "Moneda", "Total", "Diferencia",
                        "Motivos", "Revisión", "Decisión"]
        st.dataframe(view, hide_index=True, width="stretch", height=280)
        labels = dict(zip(cases["case_id"], cases["status_label"]))
        cid = st.selectbox("Abrir caso", cases["case_id"], key="case_pick", format_func=lambda x: f"{x} · {labels[x]}")
        try:
            d = client.case(run_id, cid)
        except ApiError as e:
            fail(e)
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
            dec = st.selectbox("Decisión", [x.value for x in Decision], key="res_decision")
            who = st.text_input("Analista", key="res_analyst")
            why = st.text_area("Justificación", key="res_comment")
            if st.form_submit_button("Registrar decisión"):
                try:
                    client.resolve(run_id, cid, dec, who, why)
                    st.rerun()
                except ApiError as e:
                    st.error(f"Decisión no registrada ({e.status}): {e.detail}")

with tab_exp:
    st.subheader("Exportar expediente")
    st.write(f"Incluye {len(cases)} casos de la vista filtrada con decisión humana, motivos, referencias a las filas "
             "fuente, hash SHA-256 de cada archivo de entrada y versión de reglas.")
    st.caption("Formatos: CSV, JSON y HTML imprimible (para PDF, imprimir el HTML).")
    export_key = (run_id, tuple(f.query_params()))
    if st.button("Generar expediente"):
        try:
            st.session_state["export"] = (export_key, client.export(run_id, f))
        except ApiError as e:
            fail(e)
    ready = st.session_state.get("export")
    if ready and ready[0] == export_key:
        st.download_button("Descargar expediente (.zip)", ready[1], file_name=f"expediente_{run_id}.zip",
                           mime="application/zip", type="primary")
    elif ready:
        st.caption("Los filtros cambiaron: vuelva a generar el expediente.")

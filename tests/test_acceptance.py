"""Pruebas de aceptación T-I19-01 … T-I19-05 (criterios AC-I19-01 … AC-I19-05)."""
import io
import json
import zipfile

import pandas as pd
import pytest
from conftest import GR, PO, invoice

from procurematch.engine import summarize
from procurematch.service import Service
from procurematch.store import Store
from procurematch.views import Filters, apply, kpis


def case(res, row_no=1) -> dict:
    return res.cases.set_index("row_no").loc[row_no].to_dict()


# ---------------------------------------------------------------- T-I19-01 · AC-I19-02 (positivo)
def test_T_I19_01_exact_match_is_MATCH(make_case):
    c = case(make_case(invs=[invoice()]))
    assert c["status"] == "MATCH" and c["reasons"] == ""
    assert c["qty_diff"] == 0 and c["price_diff_minor"] == 0 and c["amount_diff_minor"] == 0
    assert c["po_ref"] == "purchase_orders.csv:fila 1" and c["receipt_refs"] == "goods_receipts.csv:fila 1"


def test_T_I19_01_all_clean_fixture_rows_match(sample_run, sample_dir):
    labels = pd.read_csv(sample_dir / "expected_labels.csv")
    m = sample_run.cases.merge(labels, on="row_no")
    clean = m[m["scenario"].str.startswith("CLEAN")]
    assert len(clean) == 640 and (clean["status"] == "MATCH").all()


def test_T_I19_01_partial_receipts_and_billing_still_match(make_case):
    grs = [{**GR, "qty_received": "4"}, {**GR, "receipt_id": "GR-2", "qty_received": "6", "receipt_date": "2026-01-25"}]
    invs = [invoice(qty_billed=4), invoice(invoice_id="F-2", qty_billed=6, invoice_date="2026-02-05")]
    res = make_case(grs=grs, invs=invs)
    assert list(res.cases["status"]) == ["MATCH", "MATCH"]
    assert "PARTIAL_RECEIPTS" in case(res, 1)["notes"] and "PARTIAL_BILLING" in case(res, 2)["notes"]


# ---------------------------------------------------------------- T-I19-02 · AC-I19-02 (diferencia monetaria)
def test_T_I19_02_price_out_of_tolerance_is_EXCEPTION_with_auditable_amounts(make_case):
    res = make_case(invs=[invoice(unit_price_minor=10500)])   # +5 %; tolerancia max(100, 0.5 %·10000=50) = 100
    c = case(res)
    assert c["status"] == "EXCEPTION" and c["reasons"] == "PRICE_OUT_OF_TOLERANCE"
    assert c["price_diff_minor"] == 500 and c["price_tolerance_minor"] == 100
    assert c["amount_expected_minor"] == 116000 and c["total_minor"] == 121800 and c["amount_diff_minor"] == 5800
    assert res.manifest()["rules"]["version"] == "R1.0.0"


@pytest.mark.parametrize("delta,status", [(100, "MATCH"), (101, "EXCEPTION"), (-100, "MATCH"), (-101, "EXCEPTION")])
def test_T_I19_02_tolerance_boundary(make_case, delta, status):
    c = case(make_case(invs=[invoice(unit_price_minor=10000 + delta)]))
    assert c["status"] == status
    if status == "MATCH":
        assert c["notes"] == "PRICE_WITHIN_TOLERANCE"


@pytest.mark.parametrize("kw,grs,reason", [
    (dict(qty_billed=12), None, "QTY_EXCEEDS_RECEIVED"),
    (dict(), [GR, {**GR, "receipt_id": "GR-R", "qty_received": "2", "movement": "RETURN"}], "QTY_EXCEEDS_RECEIVED"),
    (dict(tax_minor=16500, total_minor=116500), None, "TAX_MISMATCH"),
    (dict(total_minor=116320), None, "TOTAL_ARITHMETIC_MISMATCH"),
    (dict(supplier_id="S002"), None, "SUPPLIER_MISMATCH"),
    (dict(sku="SKU-9"), None, "SKU_MISMATCH"),
    (dict(invoice_date="2026-01-05"), None, "INVOICE_BEFORE_PO"),
])
def test_T_I19_02_documentary_and_monetary_exceptions(make_case, kw, grs, reason):
    c = case(make_case(grs=grs, invs=[invoice(**kw)]))
    assert c["status"] == "EXCEPTION" and reason in c["reasons"].split(";")


def test_T_I19_02_injected_differences_detected_to_the_cent(sample_run, sample_dir):
    labels = pd.read_csv(sample_dir / "expected_labels.csv")
    m = sample_run.cases.merge(labels, on="row_no")
    inj = m[m["injected_diff_minor"] != 0]
    assert len(inj) == 24
    assert (inj["amount_diff_minor"].astype("int64") == inj["injected_diff_minor"]).all()


# ---------------------------------------------------------------- T-I19-03 · AC-I19-03
def test_T_I19_03_duplicate_invoice_goes_to_review_and_is_not_linked(make_case):
    res = make_case(invs=[invoice(), invoice()])
    assert list(res.cases["status"]) == ["REVIEW", "REVIEW"]
    assert all(r == "DUPLICATE_INVOICE" for r in res.cases["reasons"])
    assert res.cases["qty_billable"].isna().all()   # no consume cantidad recibida


def test_T_I19_03_ambiguous_candidates_go_to_review_without_link(make_case):
    pos = [PO, {**PO, "po_line": "2"}]
    res = make_case(pos=pos, grs=[GR, {**GR, "receipt_id": "GR-2", "po_line": "2"}], invs=[invoice(po_line="")])
    c = case(res)
    assert c["status"] == "REVIEW" and c["reasons"] == "AMBIGUOUS_PO_CANDIDATES" and c["po_ref"] is None


def test_T_I19_03_duplicate_po_line_is_ambiguous_not_matched(make_case):
    c = case(make_case(pos=[PO, {**PO}], invs=[invoice()]))
    assert c["status"] == "REVIEW" and "AMBIGUOUS_PO_CANDIDATES" in c["reasons"]


def test_T_I19_03_credit_note_needs_human(make_case):
    c = case(make_case(invs=[invoice(document_type="CREDIT_NOTE", qty_billed=-1)]))
    assert c["status"] == "REVIEW" and "CREDIT_NOTE_WITHOUT_REFERENCE" in c["reasons"]


# ---------------------------------------------------------------- T-I19-04 · AC-I19-01
@pytest.mark.parametrize("kw,code", [
    (dict(currency="pesos"), "currency:INVALID_CURRENCY"),
    (dict(invoice_date="2026-02-31"), "invoice_date:INVALID_DATE"),
    (dict(invoice_id=""), "invoice_id:MISSING_KEY"),
    (dict(po_id=""), "po_id:MISSING_KEY"),
    (dict(total_minor="1,160.00"), "total_minor:INVALID_NUMBER"),
    (dict(document_type="RECIBO"), "document_type:INVALID_DOCUMENT_TYPE"),
])
def test_T_I19_04_invalid_input_identified_by_file_and_row(make_case, kw, code):
    res = make_case(invs=[invoice(), invoice(**{"invoice_id": "F-2", **kw})])
    assert len(res.cases) == 2                                  # no se elimina nada
    c = case(res, 2)
    assert c["status"] == "BLOCKED" and code in c["reasons"].split(";")
    iss = res.ingested.issues
    assert ((iss["file"] == "invoice_lines.csv") & (iss["row_no"] == 2)).any()
    assert case(res, 1)["status"] == "MATCH"


@pytest.mark.parametrize("kw,reason,status", [
    (dict(currency="EUR"), "CURRENCY_NOT_ADMITTED", "BLOCKED"),
    (dict(currency="USD"), "CURRENCY_MISMATCH", "EXCEPTION"),
    (dict(invoice_date="2026-05-01"), "FUTURE_DATED", "BLOCKED"),
    (dict(supplier_id="S999"), "UNKNOWN_SUPPLIER", "BLOCKED"),
    (dict(qty_billed=-5), "NON_POSITIVE_QTY", "BLOCKED"),
    (dict(po_id="PO-X"), "PO_NOT_FOUND", "UNMATCHED"),
])
def test_T_I19_04_incompatible_currency_or_date_never_false_match(make_case, kw, reason, status):
    c = case(make_case(invs=[invoice(**kw)]))
    assert c["status"] == status and reason in c["reasons"].split(";")


def test_T_I19_04_invalid_po_row_blocks_its_invoices(make_case):
    res = make_case(pos=[{**PO, "po_date": "2026-13-40"}], invs=[invoice()])
    assert case(res)["status"] == "BLOCKED" and "PO_LINE_INVALID" in case(res)["reasons"]
    assert (res.ingested.issues["file"] == "purchase_orders.csv").any()


def test_T_I19_04_no_receipt_is_unmatched(make_case):
    c = case(make_case(grs=[], invs=[invoice()]))
    assert c["status"] == "UNMATCHED" and c["reasons"] == "NO_RECEIPT"


# ---------------------------------------------------------------- T-I19-05 · AC-I19-04 / AC-I19-05
def test_T_I19_05_funnel_partitions_without_mixing_categories(sample_run):
    s = summarize(sample_run.cases)
    st = s["por_estado"]
    assert sum(st.values()) == s["lineas"] == len(sample_run.cases)
    fun = {x["etapa"]: x["lineas"] for x in s["embudo"]}
    assert fun["Aptos"] == fun["Recibidos"] - st["BLOCKED"]
    assert fun["Vinculados 3-way"] == fun["Aptos"] - st["REVIEW"] - st["UNMATCHED"]
    assert fun["Conciliados"] == fun["Vinculados 3-way"] - st["EXCEPTION"]
    assert {m["moneda"] for m in s["montos_por_moneda"]} == {"MXN", "USD"}  # nunca se suman monedas


def test_T_I19_05_ui_filters_reproduce_engine_counts_in_sql(sample_run):
    store = Store(":memory:")
    store.save_run(sample_run)
    cases = store.cases(sample_run.run_id)
    for f, where, params in [
        (Filters(), "TRUE", []),
        (Filters(currencies=["USD"]), "currency = ?", ["USD"]),
        (Filters(suppliers=["S003", "S010"]), "supplier_id IN (?, ?)", ["S003", "S010"]),
        (Filters(statuses=["EXCEPTION"], currencies=["MXN"]), "status = ? AND currency = ?", ["EXCEPTION", "MXN"]),
        (Filters(reasons=["PRICE_OUT_OF_TOLERANCE"]), "list_contains(string_split(reasons, ';'), ?)",
         ["PRICE_OUT_OF_TOLERANCE"]),
    ]:
        ui = kpis(apply(cases, f))["por_estado"]
        sql = dict(store.count_sql(sample_run.run_id, where, params).itertuples(index=False))
        assert {k: v for k, v in ui.items() if v} == sql, f
    parts = sum(len(apply(cases, Filters(currencies=[c]))) for c in cases["currency"].dropna().unique())
    assert parts + int(cases["currency"].isna().sum()) == len(cases)


def test_T_I19_05_dashboard_metrics_equal_engine(monkeypatch, sample_run):
    monkeypatch.setenv("KPM_DB", ":memory:")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(__import__("conftest").ROOT / "dashboard" / "app.py"), default_timeout=120).run()
    assert not at.exception
    metric = {m.label: m.value for m in at.metric}
    st = sample_run.summary["por_estado"]
    assert int(metric["Líneas"]) == len(sample_run.cases)
    assert int(metric["Conciliadas"]) == st["MATCH"] and int(metric["Discrepantes"]) == st["EXCEPTION"]
    at.multiselect(key="f_currency").set_value(["USD"]).run()
    usd = sample_run.cases[sample_run.cases["currency"] == "USD"]
    assert int({m.label: m.value for m in at.metric}["Líneas"]) == len(usd)


def test_T_I19_05_export_carries_decision_reasons_version_and_refs(sample_dir, sample_run):
    from procurematch.schemas import Resolution
    svc = Service(sample_dir, ":memory:")
    res = svc.new_run(sample_run.as_of)
    assert res.run_id == sample_run.run_id                      # determinista
    exc = svc.cases(res.run_id, Filters(statuses=["EXCEPTION"]))
    target = exc.iloc[0]["case_id"]
    svc.resolve(res.run_id, target, Resolution(decision="REMITIR", analyst="ana.cxp", comment="precio no pactado"))
    z = zipfile.ZipFile(io.BytesIO(svc.export(res.run_id, Filters(statuses=["EXCEPTION"]))))
    name = lambda f: next(n for n in z.namelist() if n.endswith(f))  # noqa: E731
    rows = pd.read_csv(z.open(name("casos.csv")))
    man = json.loads(z.read(name("manifest.json")))
    assert len(rows) == len(exc) == man["exported_cases"]
    hit = rows.set_index("case_id").loc[target]
    assert hit["decision"] == "REMITIR" and hit["analyst"] == "ana.cxp" and hit["review_state"] == "RESUELTO"
    assert rows["reasons"].notna().all() and rows["invoice_ref"].str.contains("fila").all()
    assert rows["po_ref"].notna().all() and rows["receipt_refs"].notna().all()
    assert man["rules"]["version"] == "R1.0.0" and len(man["rules"]["sha256"]) == 64
    import hashlib
    for f, meta in man["sources"].items():
        assert meta["sha256"] == hashlib.sha256((sample_dir / f).read_bytes()).hexdigest()
    assert man["authority"] == {"payments_executed": False, "erp_postings": False, "purchase_orders_created": False}
    assert not any(c in rows.columns for c in ("payment_id", "payment_date", "journal_entry"))
    assert man["files_sha256"]["casos.csv"] == hashlib.sha256(z.read(name("casos.csv"))).hexdigest()
    assert b"No ejecuta pagos" in z.read(name("expediente.html"))

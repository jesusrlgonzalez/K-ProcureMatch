"""El dashboard es un cliente de la API: se prueba contra un servidor HTTP real y sin acceso al motor."""
import ast
import io
import zipfile
from datetime import date

import pytest
from conftest import ROOT
from fastapi.testclient import TestClient

from procurematch.api import create_app
from procurematch.client import ApiClient, ApiError
from procurematch.filters import Filters
from procurematch.service import Service

SAMPLE_FILES = ("purchase_orders", "goods_receipts", "invoice_lines", "suppliers")
ALLOWED = {"procurematch.client", "procurematch.filters", "procurematch.schemas"}


def test_dashboard_only_talks_to_the_api():
    tree = ast.parse((ROOT / "dashboard" / "app.py").read_text(encoding="utf-8"))
    used = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    used |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    own = {m for m in used if m.startswith("procurematch")}
    assert own <= ALLOWED, f"el dashboard no debe usar el motor directamente: {sorted(own - ALLOWED)}"
    assert not used & {"duckdb", "procurematch.engine", "procurematch.store", "procurematch.service"}


def test_dashboard_registers_decision_and_exports_through_api(dashboard):
    at = dashboard.run()
    at.selectbox(key="case_pick").set_value("L00661").run()
    assert any("PRICE_OUT_OF_TOLERANCE" in w.value for w in at.warning)
    at.selectbox(key="res_decision").set_value("REMITIR")
    at.text_input(key="res_analyst").input("ana.cxp")
    at.text_area(key="res_comment").input("precio no pactado")
    next(b for b in at.button if b.label == "Registrar decisión").click().run()
    assert not at.exception and not at.error
    assert any("REMITIR · ana.cxp" in s.value for s in at.success)
    next(b for b in at.button if b.label == "Generar expediente").click().run()
    assert [b.label for b in at.get("download_button")] == ["Descargar expediente (.zip)"]


def test_dashboard_rejects_invalid_decision_from_api(dashboard):
    at = dashboard.run()
    at.selectbox(key="case_pick").set_value("L00001").run()
    next(b for b in at.button if b.label == "Registrar decisión").click().run()   # sin analista
    assert any("Decisión no registrada (422)" in e.value for e in at.error)


def test_dashboard_shows_clear_error_without_api(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("KPM_API_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=60).run()
    assert any("No hay conexión con la API" in e.value for e in at.error)
    assert not at.metric


# ---------------------------------------------------------------- API usada por el dashboard
@pytest.fixture
def client(sample_dir, tmp_path):
    svc = Service(sample_dir, ":memory:", upload_dir=tmp_path / "uploads")
    return ApiClient("http://testserver", http=TestClient(create_app(svc)), page_size=100)


def sample_bytes(sample_dir):
    return {n: (sample_dir / f"{n}.csv").read_bytes() for n in SAMPLE_FILES}


def test_upload_dataset_runs_like_the_demo(client, sample_dir):
    demo = client.new_run(date(2026, 3, 31))
    up = client.upload(sample_bytes(sample_dir))
    assert len(up["dataset_id"]) == 16 and set(up["fuentes"]) >= {"invoice_lines.csv", "purchase_orders.csv"}
    again = client.upload(sample_bytes(sample_dir))
    assert again["dataset_id"] == up["dataset_id"]                  # el id depende solo del contenido
    run = client.new_run(date(2026, 3, 31), up["dataset_id"])
    assert run["run_id"] == demo["run_id"] and run["summary"]["por_estado"] == demo["summary"]["por_estado"]
    assert client.data_quality(run["run_id"])[0].shape[0] == 4      # cobertura de las 4 fuentes


@pytest.mark.parametrize("mutate,status,text", [
    (lambda f: f.pop("invoice_lines"), 422, ""),
    (lambda f: f.update(invoice_lines=b"folio,importe\nF-1,10\n"), 422, "faltan columnas"),
    (lambda f: f.update(purchase_orders=b"\x00\xff\xfe binario"), 422, "purchase_orders.csv"),
])
def test_upload_rejects_invalid_sources(client, sample_dir, mutate, status, text):
    files = sample_bytes(sample_dir)
    mutate(files)
    with pytest.raises(ApiError) as e:
        client.upload(files)
    assert e.value.status == status and text in e.value.detail


def test_upload_size_limit_and_unknown_datasets(client, sample_dir, monkeypatch):
    monkeypatch.setattr(Service, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(ApiError) as e:
        client.upload(sample_bytes(sample_dir))
    assert e.value.status == 413
    for bad in ("0000000000000000", "../../etc", "demo2"):
        with pytest.raises(ApiError) as e:
            client.new_run(date(2026, 3, 31), bad)
        assert e.value.status == 404


def test_client_pagination_and_filters_match_summary(client):
    rid = client.new_run(date(2026, 3, 31))["run_id"]
    every = client.cases(rid)                                       # 749 casos en páginas de 100
    assert len(every) == 749 and every["case_id"].is_unique
    for f in (Filters(currencies=["MXN"]), Filters(statuses=["EXCEPTION", "REVIEW"]),
              Filters(date_from=date(2026, 2, 1), date_to=date(2026, 2, 28), include_undated=False)):
        s, cases = client.summary(rid, f), client.cases(rid, f)
        assert s["lineas"] == len(cases) == sum(s["por_estado"].values())
    fac = client.facets(rid)
    assert fac["monedas"][:3] == ["EUR", "MXN", "USD"] and fac["facturas_sin_fecha_valida"] == 4
    assert client.manifest(rid)["rules"]["version"] == client.rules()["version"] == "R1.0.0"
    z = zipfile.ZipFile(io.BytesIO(client.export(rid, Filters(currencies=["USD"]))))
    assert any(n.endswith("manifest.json") for n in z.namelist())

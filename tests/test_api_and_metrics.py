import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from procurematch.api import create_app
from procurematch.evaluate import evaluate
from procurematch.rules import load_rules
from procurematch.service import Service


@pytest.fixture
def client(sample_dir):
    return TestClient(create_app(Service(sample_dir, ":memory:")))


def test_api_flow(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.post("/runs", json={"as_of": "2026-03-31"})
    assert r.status_code == 201
    rid = r.json()["run_id"]
    s = client.get(f"/runs/{rid}/summary", params={"currency": "MXN"}).json()
    assert s["filters"] == {"currencies": ["MXN"]} and sum(s["por_estado"].values()) == s["lineas"]
    page = client.get(f"/runs/{rid}/cases", params={"status": "REVIEW", "limit": 5}).json()
    assert page["total"] == 16 and len(page["items"]) == 5
    cid = page["items"][0]["case_id"]
    d = client.get(f"/runs/{rid}/cases/{cid}").json()
    assert d["documentos"]["factura"][0]["fuente"].startswith("invoice_lines.csv:fila")
    assert {x["campo"] for x in d["comparacion"]} >= {"Cantidad", "Precio unitario", "Impuesto", "Total"}
    ok = client.post(f"/runs/{rid}/cases/{cid}/resolution", json={"decision": "RECHAZAR", "analyst": "ana"})
    assert ok.status_code == 201
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/runs/{rid}/export", params={"status": "REVIEW"}).content))
    assert any(n.endswith("expediente.html") for n in z.namelist())


def test_api_rejects_unknown_and_payment_like_actions(client):
    rid = client.post("/runs", json={"as_of": "2026-03-31"}).json()["run_id"]
    assert client.get("/runs/nope/summary").status_code == 404
    assert client.get(f"/runs/{rid}/cases/L99999").status_code == 404
    assert client.post(f"/runs/{rid}/cases/L00001/resolution", json={"decision": "PAGAR", "analyst": "ana"}).status_code == 422
    assert client.post(f"/runs/{rid}/cases/L00001/resolution", json={"decision": "RECHAZAR", "analyst": ""}).status_code == 422
    assert not [r for r in client.app.routes if any(w in r.path for w in ("pay", "pago", "erp"))]


def test_stale_sources_are_detected(tmp_path, sample_dir):
    import shutil
    for f in sample_dir.iterdir():
        shutil.copy(f, tmp_path / f.name)
    svc = Service(tmp_path, ":memory:")
    rid = svc.new_run(__import__("datetime").date(2026, 3, 31)).run_id
    svc._cache.clear()
    with (tmp_path / "invoice_lines.csv").open("a", encoding="utf-8") as fh:
        fh.write("F-NUEVA,1,S001,PO-00001,1,SKU-1,2026-02-01,1,100,16,116,MXN,INVOICE,\n")
    c = TestClient(create_app(svc))
    assert c.get(f"/runs/{rid}/export").status_code == 409


def test_rules_version_and_hash_change_the_run_id(tmp_path, sample_dir):
    from procurematch.engine import run
    rules = (load_rules().path, (__import__("conftest").ROOT / "rules" / "rules_v1.yaml").read_text(encoding="utf-8"))
    alt = tmp_path / "rules_alt.yaml"
    alt.write_text(rules[1].replace("unit_price_abs_minor: 100", "unit_price_abs_minor: 200"), encoding="utf-8")
    a = run(sample_dir, as_of=__import__("datetime").date(2026, 3, 31))
    b = run(sample_dir, as_of=__import__("datetime").date(2026, 3, 31), rules_path=alt)
    assert a.run_id != b.run_id and a.rules.sha256 != b.rules.sha256
    bad = tmp_path / "rules_bad.yaml"
    bad.write_text(rules[1].replace("qty_units: 0", "qty_units: -1"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_rules(bad)


def test_metrics_vs_labels_and_baseline(sample_run, sample_dir):
    rep = evaluate(sample_run, sample_dir)
    cur, v0 = rep["motor_actual"], rep["motor_v0"]
    assert cur["falsas_coincidencias"] == 0 and cur["exactitud"] == 1.0
    money = cur["diferencia_monetaria_por_moneda"]
    assert {m["moneda"] for m in money} == {"MXN", "USD"} and sum(m["casos"] for m in money) == 24
    assert all(m["inyectada_minor"] == m["detectada_minor"] and m["casos_detectados_exactos"] == m["casos"]
               for m in money)
    assert v0["falsas_coincidencias"] > cur["falsas_coincidencias"]          # la regla nueva no empeora
    assert cur["esfuerzo_revision"] < v0["esfuerzo_revision"] < rep["revision_manual_total"]["esfuerzo_revision"]

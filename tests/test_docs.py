"""La documentación no puede quedar desalineada del motor."""
from conftest import ROOT

from procurematch.engine import NOTES, REASONS


def test_every_reason_code_is_documented():
    doc = (ROOT / "docs" / "reglas.md").read_text(encoding="utf-8")
    missing = [c for c in [*REASONS, *NOTES] if f"`{c}`" not in doc]
    assert not missing, f"códigos sin documentar en docs/reglas.md: {missing}"


def test_every_reason_code_is_implemented_in_sql():
    sql = (ROOT / "src" / "procurematch" / "sql" / "reconcile.sql").read_text(encoding="utf-8")
    missing = [c for c in [*REASONS, *NOTES] if f"'{c}'" not in sql]
    assert not missing, f"motivos del catálogo sin regla en reconcile.sql: {missing}"

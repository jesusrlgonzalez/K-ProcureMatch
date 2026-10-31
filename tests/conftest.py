import csv
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from procurematch.engine import run  # noqa: E402
from procurematch.fixture import AS_OF, GR_COLS, INV_COLS, PO_COLS, build  # noqa: E402

SAMPLE = ROOT / "data" / "sample"


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    """Fixture regenerado en cada sesión y comparado byte a byte con el versionado en data/sample."""
    out = tmp_path_factory.mktemp("sample")
    build(out)
    for f in ("purchase_orders.csv", "goods_receipts.csv", "invoice_lines.csv", "suppliers.csv", "expected_labels.csv"):
        assert (out / f).read_bytes() == (SAMPLE / f).read_bytes(), f"data/sample/{f} no coincide con el generador"
    return out


@pytest.fixture(scope="session")
def sample_run(sample_dir):
    return run(sample_dir, as_of=AS_OF)


PO = dict(po_id="PO-1", po_line="1", supplier_id="S001", sku="SKU-1", qty_ordered="10", unit_price_minor="10000",
          tax_rate_bp="1600", currency="MXN", po_date="2026-01-10")
GR = dict(receipt_id="GR-1", po_id="PO-1", po_line="1", sku="SKU-1", qty_received="10", receipt_date="2026-01-20",
          movement="RECEIPT")
INV = dict(invoice_id="F-1", invoice_line="1", supplier_id="S001", po_id="PO-1", po_line="1", sku="SKU-1",
           invoice_date="2026-02-01", qty_billed="10", unit_price_minor="10000", tax_minor="16000",
           total_minor="116000", currency="MXN", document_type="INVOICE", original_invoice="")


def _write(path: Path, cols, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def make_case(tmp_path):
    """Construye un conjunto mínimo de tres fuentes y devuelve la corrida."""
    def _make(pos=None, grs=None, invs=None, suppliers=("S001", "S002"), as_of=date(2026, 3, 31)):
        _write(tmp_path / "purchase_orders.csv", PO_COLS, pos if pos is not None else [PO])
        _write(tmp_path / "goods_receipts.csv", GR_COLS, grs if grs is not None else [GR])
        _write(tmp_path / "invoice_lines.csv", INV_COLS, invs if invs is not None else [INV])
        if suppliers is not None:
            _write(tmp_path / "suppliers.csv", ["supplier_id", "legal_name", "alias"],
                   [{"supplier_id": s, "legal_name": s, "alias": s} for s in suppliers])
        return run(tmp_path, as_of=as_of)
    return _make


def invoice(**kw):
    sub = int(kw.get("qty_billed", INV["qty_billed"])) * int(kw.get("unit_price_minor", INV["unit_price_minor"]))
    tax = (sub * 1600 + 5000) // 10000
    base = {**INV, "tax_minor": str(tax), "total_minor": str(sub + tax)}
    base.update({k: str(v) for k, v in kw.items()})
    return base

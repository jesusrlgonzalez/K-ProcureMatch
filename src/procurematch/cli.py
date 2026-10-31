"""Conciliación por línea de comandos: genera el expediente sin interfaz."""
from __future__ import annotations

import argparse
import io
import json
import zipfile
from datetime import date
from pathlib import Path

from .service import Service


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="carpeta con purchase_orders.csv, goods_receipts.csv, invoice_lines.csv")
    ap.add_argument("--as-of", help="fecha de corte AAAA-MM-DD (por defecto, hoy)")
    ap.add_argument("--out", required=True, help="carpeta de salida del expediente")
    a = ap.parse_args()
    svc = Service(a.source, ":memory:")
    res = svc.new_run(date.fromisoformat(a.as_of) if a.as_of else None)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(svc.export(res.run_id))) as z:
        for n in z.namelist():
            (out / Path(n).name).write_bytes(z.read(n))
    print(json.dumps({"run_id": res.run_id, "rules": res.rules.version, **res.summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

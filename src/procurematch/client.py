"""Cliente HTTP de la API de K-ProcureMatch.

Es la única vía por la que el dashboard accede al sistema: no importa el motor, el almacenamiento
ni DuckDB. Cualquier otra herramienta (script, notebook, integración) puede usarlo igual.
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pandas as pd

from .filters import Filters

SOURCE_FILES = {"purchase_orders": "purchase_orders.csv", "goods_receipts": "goods_receipts.csv",
                "invoice_lines": "invoice_lines.csv", "suppliers": "suppliers.csv"}
REQUIRED_SOURCES = ("purchase_orders", "goods_receipts", "invoice_lines")


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"[{status}] {detail}")
        self.status, self.detail = status, detail


class ApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, timeout: float = 120.0,
                 http: httpx.Client | None = None, page_size: int = 1000):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(base_url=self.base_url, timeout=timeout)
        self.page_size = page_size

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ApiError(0, f"sin conexión con la API en {self.base_url}: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(r.status_code, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
        return r

    def _get(self, path: str, f: Filters | None = None, **extra) -> dict:
        params = (f.query_params() if f else []) + [(k, str(v)) for k, v in extra.items()]
        return self._call("GET", path, params=params).json()

    # ------------------------------------------------------------ sistema
    def health(self) -> dict:
        return self._get("/health")

    def rules(self) -> dict:
        return self._get("/rules")

    # ------------------------------------------------------------ fuentes y corridas
    def upload(self, files: dict[str, bytes]) -> dict:
        parts = {name: (SOURCE_FILES[name], data, "text/csv") for name, data in files.items() if data}
        return self._call("POST", "/datasets", files=parts).json()

    def new_run(self, as_of: date | None = None, dataset_id: str = "demo") -> dict:
        body = {"dataset_id": dataset_id, "as_of": as_of.isoformat() if as_of else None}
        return self._call("POST", "/runs", json=body).json()

    def manifest(self, run_id: str) -> dict:
        return self._get(f"/runs/{run_id}/manifest")

    def facets(self, run_id: str) -> dict:
        return self._get(f"/runs/{run_id}/facets")

    def data_quality(self, run_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        d = self._get(f"/runs/{run_id}/data-quality")
        return pd.DataFrame(d["cobertura"]), pd.DataFrame(d["incidencias"], columns=["file", "row_no", "field", "code",
                                                                                     "value"])

    # ------------------------------------------------------------ análisis
    def summary(self, run_id: str, f: Filters | None = None) -> dict:
        return self._get(f"/runs/{run_id}/summary", f)

    def cases(self, run_id: str, f: Filters | None = None) -> pd.DataFrame:
        items, offset = [], 0
        while True:
            page = self._get(f"/runs/{run_id}/cases", f, limit=self.page_size, offset=offset)
            items += page["items"]
            offset += len(page["items"])
            if offset >= page["total"] or not page["items"]:
                return pd.DataFrame(items)

    def case(self, run_id: str, case_id: str) -> dict:
        return self._get(f"/runs/{run_id}/cases/{case_id}")

    def resolve(self, run_id: str, case_id: str, decision: str, analyst: str, comment: str = "") -> dict:
        body = {"decision": decision, "analyst": analyst, "comment": comment}
        return self._call("POST", f"/runs/{run_id}/cases/{case_id}/resolution", json=body).json()

    def export(self, run_id: str, f: Filters | None = None) -> bytes:
        return self._call("GET", f"/runs/{run_id}/export", params=f.query_params() if f else []).content

"""API REST de solo consulta y registro de decisiones. No expone operaciones de pago ni de ERP.

Configuración por variables de entorno:
  KPM_DATA_DIR  carpeta con las fuentes (por defecto data/sample)
  KPM_DB        base DuckDB de corridas y decisiones (por defecto data/kpm.duckdb)
"""
from __future__ import annotations

import os
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from . import __version__
from .ingest import SchemaError
from .schemas import Resolution
from .service import Service, StaleRun
from .store import UnknownCase
from .views import Filters


class RunRequest(BaseModel):
    as_of: date | None = None


def create_app(service: Service | None = None) -> FastAPI:
    svc = service or Service(os.environ.get("KPM_DATA_DIR", "data/sample"), os.environ.get("KPM_DB", "data/kpm.duckdb"))
    app = FastAPI(title="K-ProcureMatch", version=__version__,
                  description="Conciliación 3-way de órdenes de compra, recepciones y facturas. "
                              "Soporte a decisión: no aprueba pagos ni registra asientos.")

    def filters(supplier: list[str] = Query(default=[]), currency: list[str] = Query(default=[]),
                status: list[str] = Query(default=[]), reason: list[str] = Query(default=[]),
                review_state: list[str] = Query(default=[]), date_from: date | None = None,
                date_to: date | None = None) -> Filters:
        return Filters(supplier, currency, status, reason, review_state, date_from, date_to)

    def known(run_id: str) -> str:
        try:
            svc.store.manifest(run_id)
        except KeyError:
            raise HTTPException(404, "corrida no encontrada") from None
        return run_id

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": __version__}

    @app.post("/runs", status_code=201)
    def create_run(req: RunRequest):
        try:
            res = svc.new_run(req.as_of)
        except SchemaError as e:
            raise HTTPException(422, str(e)) from None
        return {"run_id": res.run_id, "manifest": res.manifest(), "summary": res.summary}

    @app.get("/runs")
    def list_runs():
        return svc.store.runs().to_dict("records")

    @app.get("/runs/{run_id}/summary")
    def summary(run_id: str = Depends(known), f: Filters = Depends(filters)):
        return {"filters": f.as_dict(), **svc.summary(run_id, f)}

    @app.get("/runs/{run_id}/cases")
    def cases(run_id: str = Depends(known), f: Filters = Depends(filters), limit: int = Query(100, ge=1, le=1000),
              offset: int = Query(0, ge=0)):
        df = svc.cases(run_id, f)
        page = df.iloc[offset:offset + limit].astype(object).where(df.notna(), None)
        return {"total": len(df), "items": page.to_dict("records")}

    @app.get("/runs/{run_id}/cases/{case_id}")
    def case(case_id: str, run_id: str = Depends(known)):
        try:
            return svc.case_detail(run_id, case_id)
        except KeyError:
            raise HTTPException(404, "caso no encontrado") from None
        except StaleRun:
            raise HTTPException(409, "las fuentes cambiaron desde esta corrida") from None

    @app.post("/runs/{run_id}/cases/{case_id}/resolution", status_code=201)
    def resolve(case_id: str, body: Resolution, run_id: str = Depends(known)):
        try:
            return svc.resolve(run_id, case_id, body)
        except UnknownCase:
            raise HTTPException(404, "caso no encontrado") from None

    @app.get("/runs/{run_id}/export")
    def export(run_id: str = Depends(known), f: Filters = Depends(filters)):
        try:
            data = svc.export(run_id, f)
        except StaleRun:
            raise HTTPException(409, "las fuentes cambiaron desde esta corrida") from None
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="expediente_{run_id}.zip"'})

    return app
# Arranque: uvicorn procurematch.api:create_app --factory
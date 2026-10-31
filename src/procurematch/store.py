"""Persistencia en DuckDB: corridas, casos y decisiones humanas (historial sin borrado)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from .engine import NUMERIC_COLUMNS, RunResult
from .schemas import Resolution

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id VARCHAR PRIMARY KEY, created_at VARCHAR, as_of VARCHAR,
    rules_version VARCHAR, rules_sha256 VARCHAR, manifest VARCHAR);
CREATE TABLE IF NOT EXISTS resolutions (run_id VARCHAR, case_id VARCHAR, decision VARCHAR, analyst VARCHAR,
    comment VARCHAR, resolved_at VARCHAR);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS dataset_id VARCHAR;
"""


class UnknownCase(KeyError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = duckdb.connect(":memory:") if self.path == ":memory:" else None
        self._lock = threading.Lock()  # una conexión en memoria no se comparte entre hilos a la vez
        with self._con() as con:
            con.execute(SCHEMA)

    def _con(self):
        return _Borrowed(self._mem, self._lock) if self._mem is not None else duckdb.connect(self.path)

    def save_run(self, result: RunResult, dataset_id: str = "demo") -> None:
        """Idempotente: la misma corrida (mismas entradas, reglas y fecha de corte) produce el mismo run_id."""
        cases = result.cases.copy()
        cases.insert(0, "run_id", result.run_id)
        with self._con() as con:
            con.register("cases_df", cases)
            con.execute("CREATE TABLE IF NOT EXISTS cases AS SELECT * FROM cases_df LIMIT 0")
            con.execute("DELETE FROM cases WHERE run_id = ?", [result.run_id])
            con.execute("INSERT INTO cases SELECT * FROM cases_df")
            con.unregister("cases_df")
            con.execute("DELETE FROM runs WHERE run_id = ?", [result.run_id])
            con.execute("INSERT INTO runs (run_id, created_at, as_of, rules_version, rules_sha256, manifest, dataset_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [result.run_id, _now(), result.as_of.isoformat(), result.rules.version,
                         result.rules.sha256, json.dumps(result.manifest(), ensure_ascii=False), dataset_id])

    def runs(self) -> pd.DataFrame:
        with self._con() as con:
            return con.execute("SELECT run_id, dataset_id, created_at, as_of, rules_version FROM runs "
                               "ORDER BY created_at DESC").df()

    def dataset_of(self, run_id: str) -> str:
        with self._con() as con:
            row = con.execute("SELECT dataset_id FROM runs WHERE run_id = ?", [run_id]).fetchone()
        if not row:
            raise KeyError(run_id)
        return row[0] or "demo"

    def manifest(self, run_id: str) -> dict:
        with self._con() as con:
            row = con.execute("SELECT manifest FROM runs WHERE run_id = ?", [run_id]).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    def resolve(self, run_id: str, case_id: str, res: Resolution) -> dict:
        with self._con() as con:
            if not con.execute("SELECT 1 FROM cases WHERE run_id = ? AND case_id = ?", [run_id, case_id]).fetchone():
                raise UnknownCase(case_id)
            rec = [run_id, case_id, res.decision.value, res.analyst, res.comment, _now()]
            con.execute("INSERT INTO resolutions VALUES (?, ?, ?, ?, ?, ?)", rec)
        return dict(zip(["run_id", "case_id", "decision", "analyst", "comment", "resolved_at"], rec))

    def cases(self, run_id: str) -> pd.DataFrame:
        """Casos con la última decisión humana; estado de revisión PENDIENTE o RESUELTO."""
        q = """
        WITH last AS (
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (PARTITION BY case_id ORDER BY resolved_at DESC, rowid DESC) AS rn
                FROM resolutions WHERE run_id = ?) WHERE rn = 1)
        SELECT c.* EXCLUDE (run_id), l.decision, l.analyst, l.comment AS decision_comment, l.resolved_at,
               CASE WHEN l.decision IS NULL THEN 'PENDIENTE' ELSE 'RESUELTO' END AS review_state
        FROM cases c LEFT JOIN last l USING (case_id) WHERE c.run_id = ? ORDER BY c.row_no"""
        with self._con() as con:
            df = con.execute(q, [run_id, run_id]).df()
        for col in NUMERIC_COLUMNS:  # DuckDB entrega enteros con nulos como float
            df[col] = pd.to_numeric(df[col]).astype("Int64")
        return df

    def count_sql(self, run_id: str, where: str = "TRUE", params: list | None = None) -> pd.DataFrame:
        """Conteo independiente en SQL (para verificar que la interfaz no altera totales)."""
        with self._con() as con:
            return con.execute(f"SELECT status, count(*) AS n FROM cases WHERE run_id = ? AND ({where}) "
                               "GROUP BY status ORDER BY status", [run_id, *(params or [])]).df()


class _Borrowed:
    def __init__(self, con, lock: threading.Lock):
        self.con, self.lock = con, lock

    def __enter__(self):
        self.lock.acquire()
        return self.con

    def __exit__(self, *exc):
        self.lock.release()
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

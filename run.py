#!/usr/bin/env python3
"""Lanzador portable (Windows y Linux).

  python run.py test        pruebas automatizadas
  python run.py demo        conciliación del fixture de demostración → demo_output/
  python run.py evaluate    exactitud, falsas coincidencias y esfuerzo vs motor v0 → reports/metrics.json
  python run.py dashboard   interfaz del analista en http://localhost:8501
  python run.py api         API REST en http://127.0.0.1:8000/docs
  python run.py fixture     regenera data/sample de forma determinista
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CMDS = {
    "test": ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
    "demo": ["-m", "procurematch.cli", "--source", "data/sample", "--as-of", "2026-03-31", "--out", "demo_output"],
    "evaluate": ["-m", "procurematch.evaluate"],
    "dashboard": ["-m", "streamlit", "run", "dashboard/app.py"],
    "api": ["-m", "uvicorn", "procurematch.api:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000"],
    "fixture": ["-m", "procurematch.fixture", "--out", "data/sample"],
}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in CMDS:
        print(__doc__)
        return 2
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONPATH=os.pathsep.join(p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p))
    return subprocess.call([sys.executable, *CMDS[argv[0]]], cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

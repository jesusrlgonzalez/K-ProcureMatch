#!/usr/bin/env python3
"""Lanzador portable (Windows y Linux).

  python run.py test        pruebas automatizadas
  python run.py demo        conciliación del fixture de demostración → demo_output/
  python run.py evaluate    exactitud, falsas coincidencias y esfuerzo vs motor v0 → reports/metrics.json
  python run.py api         API REST en http://127.0.0.1:8000/docs
  python run.py dashboard   API + interfaz del analista (http://localhost:8501); el dashboard consume la API
  python run.py fixture     regenera data/sample de forma determinista

Variables: KPM_API_URL (usar una API ya en marcha), KPM_API_PORT (puerto de la API local, 8000).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = os.environ.get("KPM_API_PORT", "8000")

CMDS = {
    "test": ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
    "demo": ["-m", "procurematch.cli", "--source", "data/sample", "--as-of", "2026-03-31", "--out", "demo_output"],
    "evaluate": ["-m", "procurematch.evaluate"],
    "api": ["-m", "uvicorn", "procurematch.api:create_app", "--factory", "--host", "127.0.0.1", "--port", PORT],
    "fixture": ["-m", "procurematch.fixture", "--out", "data/sample"],
}


def _env() -> dict:
    return dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                PYTHONPATH=os.pathsep.join(p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p))


def _api_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return json.load(r).get("status") == "ok"
    except (OSError, ValueError):
        return False


def dashboard() -> int:
    env = _env()
    url = env.get("KPM_API_URL") or f"http://127.0.0.1:{PORT}"
    api = None
    if not _api_alive(url):
        if env.get("KPM_API_URL"):
            print(f"No hay API en {url}")
            return 1
        api = subprocess.Popen([sys.executable, *CMDS["api"]], cwd=ROOT, env=env)
        for _ in range(60):
            if _api_alive(url):
                break
            if api.poll() is not None:
                print("La API no pudo arrancar")
                return 1
            time.sleep(1)
        else:
            api.terminate()
            print("La API no respondió a tiempo")
            return 1
    env["KPM_API_URL"] = url
    print(f"API: {url}/docs   ·   Dashboard: http://localhost:8501")
    try:
        return subprocess.call([sys.executable, "-m", "streamlit", "run", "dashboard/app.py"], cwd=ROOT, env=env)
    finally:
        if api:
            api.terminate()
            api.wait(timeout=10)


def main(argv: list[str]) -> int:
    if argv == ["dashboard"]:
        return dashboard()
    if len(argv) != 1 or argv[0] not in CMDS:
        print(__doc__)
        return 2
    return subprocess.call([sys.executable, *CMDS[argv[0]]], cwd=ROOT, env=_env())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

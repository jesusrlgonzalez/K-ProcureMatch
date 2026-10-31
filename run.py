#!/usr/bin/env python3
"""Ejecuta la demo de forma portable (Windows y Linux).

  python run.py test    tests automatizados
  python run.py demo    demo de punta a punta con datos sintéticos; resultados en demo_output/
  python run.py api     API REST local en http://127.0.0.1:8000/docs (si el proyecto la incluye)
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO = ["-m", "i19.reconcile", "--source", "DATOS_FUENTES/04_SINTETICOS", "--out", "{OUT}"]
API = None


def main(argv: list[str]) -> int:
    cmds = {
        "test": ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
        "demo": [a.replace("{OUT}", str(ROOT / "demo_output")) for a in DEMO],
    }
    if API:
        cmds["api"] = ["-m", "uvicorn", API, "--host", "127.0.0.1", "--port", "8000"]
    if len(argv) != 1 or argv[0] not in cmds:
        print(__doc__)
        return 2
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONPATH=os.pathsep.join(p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p))
    return subprocess.call([sys.executable, *cmds[argv[0]]], cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

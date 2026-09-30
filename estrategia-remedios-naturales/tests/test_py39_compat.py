"""Compatibilidad con Python 3.9 (la Mac del usuario usa el .venv con 3.9.6).

Regresion del canario 0c464d6: `def _retry_after(r) -> float | None` en app/services/http.py (sin `from __future__ import annotations`)
reventaba AL IMPORTAR con `TypeError: unsupported operand type(s) for |`. Estos tests lo habrian detectado aunque el desarrollo corra en 3.11+.
"""
from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(p for d in ("app", "tools", "tests") for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)
CRITICAL = ["app.services.http", "app.services.dubvoice", "app.services.google_veo", "app.services.errors", "app.phases.videos",
            "app.phases.video_jobs", "app.phases.supervisor", "app.jobs", "app.autopilot"]


def _has_future(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in tree.body)


def _annotations(tree: ast.Module):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.returns is not None:
                yield n.returns
            for a in [*n.args.args, *n.args.posonlyargs, *n.args.kwonlyargs, n.args.vararg, n.args.kwarg]:
                if a is not None and a.annotation is not None:
                    yield a.annotation
        elif isinstance(n, ast.AnnAssign):
            yield n.annotation


def _has_bitor(node: ast.AST) -> bool:
    return any(isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr) for x in ast.walk(node))


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_source_has_no_python_310_syntax_and_no_runtime_union_annotations(path):
    src = path.read_text()
    tree = ast.parse(src, filename=str(path), feature_version=(3, 9))            # match/case, `with (a, b):`, etc. fallan aqui
    if not _has_future(tree):
        bad = [ast.unparse(a) for a in _annotations(tree) if _has_bitor(a)]
        assert not bad, f"{path.name}: anotaciones `X | Y` evaluadas en tiempo de ejecucion (TypeError en 3.9) sin `from __future__ import annotations`: {bad[:3]}"
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("isinstance", "issubclass") and len(n.args) == 2 and _has_bitor(n.args[1]):
            pytest.fail(f"{path.name}:{n.lineno}: isinstance(..., A | B) requiere Python 3.10+")
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "zip" and any(k.arg == "strict" for k in n.keywords):
            pytest.fail(f"{path.name}:{n.lineno}: zip(strict=) requiere Python 3.10+")
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("pairwise", "bit_count") or (isinstance(n, ast.Name) and n.id in ("aiter", "anext")):
            pytest.fail(f"{path.name}:{getattr(n, 'lineno', '?')}: API de Python 3.10+")


def test_the_exact_regression_would_be_detected():
    """El codigo del canario fallido debe ser rechazado por el detector."""
    src = "import time\n\ndef _retry_after(r) -> float | None:\n    return None\n"
    tree = ast.parse(src, feature_version=(3, 9))
    assert not _has_future(tree) and any(_has_bitor(a) for a in _annotations(tree))


@pytest.mark.parametrize("mod", CRITICAL)
def test_critical_modules_import_cleanly(mod):
    importlib.import_module(mod)


def _python39() -> str | None:
    if sys.version_info[:2] == (3, 9):
        return sys.executable
    return shutil.which("python3.9")


def test_critical_modules_import_under_a_real_python_39_if_available():
    """Importa los modulos criticos en un interprete 3.9 REAL. Si no hay uno, se omite (y se dice: no cuenta como prueba en 3.9)."""
    exe = _python39()
    if not exe:
        pytest.skip("No hay Python 3.9 en este entorno: esta comprobacion NO se ejecuto (solo corre el analisis estatico)")
    code = "import importlib,sys\n" + "".join(f"importlib.import_module({m!r})\n" for m in CRITICAL)
    import os
    env = {**os.environ, "ERN_DATA_DIR": os.environ.get("ERN_DATA_DIR", "/tmp/ern-py39")}
    r = subprocess.run([exe, "-c", code], cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
    import re
    missing = re.search(r"ModuleNotFoundError: No module named '([^']+)'", r.stderr)
    if r.returncode != 0 and missing and not missing.group(1).startswith("app"):
        pytest.skip(f"El Python 3.9 encontrado ({exe}) no tiene instalada la dependencia '{missing.group(1)}': NO se ejecuto la importacion real")
    assert r.returncode == 0, r.stderr[-1500:]

"""Ejecucion de trabajos largos en hilos, con estado visible en project.json."""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from . import store

_running: set[tuple[str, str]] = set()
_rlock = threading.Lock()


class Progress:
    def __init__(self, pid: str, name: str):
        self.pid, self.name = pid, name

    def __call__(self, message: str | None = None, progress: float | None = None):
        with store.edit(self.pid) as p:
            j = p["jobs"].setdefault(self.name, {})
            if message is not None:
                j["message"] = message
            if progress is not None:
                j["progress"] = round(progress, 3)
            j["updated"] = time.time()


def is_running(pid: str, name: str) -> bool:
    return (pid, name) in _running


def start(pid: str, name: str, fn: Callable[[Progress], None], *, sync: bool = False) -> bool:
    """Lanza fn(progress) en un hilo. Devuelve False si ya hay un trabajo igual activo."""
    with _rlock:
        if (pid, name) in _running:
            return False
        _running.add((pid, name))
    with store.edit(pid) as p:
        p["jobs"][name] = {"status": "running", "message": "Iniciando…", "progress": 0,
                           "started": time.time(), "updated": time.time(), "error": None}

    def run():
        prog = Progress(pid, name)
        try:
            fn(prog)
            status, err = "done", None
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status, err = "error", f"{type(e).__name__}: {e}"
        finally:
            with _rlock:
                _running.discard((pid, name))
        try:
            with store.edit(pid) as p:
                j = p["jobs"].setdefault(name, {})
                j.update(status=status, error=err, updated=time.time())
                if status == "done":
                    j["progress"] = 1
        except KeyError:
            pass

    if sync:
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
    return True


def reset_stale() -> None:
    """Al arrancar, los trabajos que quedaron 'running' de una sesion anterior se marcan como interrumpidos."""
    for item in store.list_projects():
        try:
            with store.edit(item["id"]) as p:
                for j in p["jobs"].values():
                    if j.get("status") == "running":
                        j.update(status="error", error="Interrumpido (la app se reinicio). Vuelve a lanzarlo.")
                for s in p.get("scenes", []):
                    for c in s.get("clips", []):
                        if c.get("status") == "running":
                            c["status"] = "error"
                            c["error"] = "Interrumpido"
                # Fase 5: un envio a medias o un job en curso NO se reenvia solo (doble cobro): se marca para reconciliar / revisar
                from .phases import video_jobs
                video_jobs.recover_after_restart(p, active=set())
        except Exception:
            pass


def force_reset(pid: str, name: str) -> None:
    """Destraba una tarea que quedo colgada: la marca como interrumpida para poder lanzarla de nuevo."""
    with _rlock:
        _running.discard((pid, name))
    with store.edit(pid) as p:
        j = p["jobs"].setdefault(name, {})
        j.update(status="error", error="Detenida manualmente. Puedes volver a lanzarla.", updated=time.time())
        for s in p.get("scenes", []):
            if s.get("img_state") == "running":
                s["img_state"], s["img_error"] = "error", "Interrumpida"
            for c in s.get("clips", []):
                if c.get("status") == "running":
                    c["status"], c["error"] = "error", "Detenido manualmente. Puedes volver a generarlo."

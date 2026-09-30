"""Persistencia de proyectos en disco (project.json) con bloqueo por proyecto."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import PROJECTS_DIR

_locks: dict[str, threading.RLock] = {}
_glock = threading.Lock()

DEFAULT_SETTINGS = {
    "claude_model": "claude-sonnet-5-5",
    "image_provider": "google",             # google | kie
    "image_model": "gemini-2.5-flash-image",  # Nano Banana
    "kie_image_model": "google/nano-banana-edit",
    "video_model": "veo3_fast",
    "video_provider": "dubvoice",           # DubVoice es el proveedor oficial de video
    "dubvoice_image_model": "nano-banana-pro",
    "image_fallback": True,
    "scene_ref_mode": "guide",              # guide (metodo de la guia, encadenado) | swap | blur | none | full
    "image_qa": True,                       # Claude revisa cada imagen y corrige hasta 2 veces
    "output_language": "es",                # idioma del video final: es | en
    "dubvoice_video_model": "veo-3.1-fast",
    "stt_provider": "local",                # local (gratis) | elevenlabs
    "whisper_model": "small.en",
    "voice_provider": "dubvoice",           # dubvoice | elevenlabs
    "unify_voice": True,
    "voice_id": None,
    "voice_name": None,
    "scene_threshold": 0.30,
    "max_scene_len": 8.0,
    "max_scenes": 40,
    "global_notes": "",
    "sub_font_size": 86,
    "sub_words_per_chunk": 3,
    "sub_margin_v": 430,
    "sub_uppercase": True,
}


def _migrate(p: dict) -> None:
    for k, v in DEFAULT_SETTINGS.items():
        p["settings"].setdefault(k, v)
    if p["settings"].get("_v", 0) < 3:          # v3: metodo de la guia (encadenado) + Nano Banana Pro
        p["settings"]["scene_ref_mode"] = "guide"
        p["settings"]["dubvoice_image_model"] = "nano-banana-pro"
        p["settings"]["_v"] = 3
    if p["settings"].get("_v", 0) < 4:          # v4: DubVoice oficial para video
        p["settings"]["video_provider"] = "dubvoice"
        if p["settings"].get("dubvoice_video_model") not in ("veo-3.1-fast", "veo-3.1", "veo-3.1-lite", "omniflash", "meta"):
            p["settings"]["dubvoice_video_model"] = "veo-3.1-fast"
        p["settings"]["_v"] = 4


def _lock(pid: str) -> threading.RLock:
    with _glock:
        return _locks.setdefault(pid, threading.RLock())


def pdir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def exists(pid: str) -> bool:
    return (pdir(pid) / "project.json").exists()


def _read(pid: str) -> dict:
    return json.loads((pdir(pid) / "project.json").read_text())


def _write(p: dict) -> None:
    f = pdir(p["id"]) / "project.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1))
    tmp.replace(f)


def create(name: str) -> dict:
    pid = time.strftime("%Y%m%d-") + uuid.uuid4().hex[:6]
    pdir(pid).mkdir(parents=True, exist_ok=True)
    p = {
        "id": pid, "name": name or "Nuevo proyecto", "created": time.time(),
        "avatar": None, "source": None,
        "settings": dict(DEFAULT_SETTINGS),
        "analysis": {"points": {"transcription": False, "translation": False,
                                "scenes": False, "prompts": False},
                     "transcript": None, "translation": None},
        "scenes": [], "final": None, "jobs": {},
    }
    _write(p)
    return p


def get(pid: str) -> dict:
    if not exists(pid):
        raise KeyError(pid)
    with _lock(pid):
        p = _read(pid)
    _migrate(p)
    return p


@contextmanager
def edit(pid: str):
    """Carga, cede el dict para mutarlo y guarda al salir (bloque corto, sin I/O de red)."""
    with _lock(pid):
        p = _read(pid)
        _migrate(p)
        yield p
        _write(p)


def list_projects() -> list[dict]:
    out = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir(), reverse=True):
            if (d / "project.json").exists():
                try:
                    p = _read(d.name)
                    out.append({"id": p["id"], "name": p["name"], "created": p["created"]})
                except Exception:
                    pass
    return out


def delete(pid: str) -> None:
    shutil.rmtree(pdir(pid), ignore_errors=True)


def path(pid: str, *parts: str) -> Path:
    f = pdir(pid).joinpath(*parts)
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def rel(pid: str, f: Path) -> str:
    return str(Path(f).relative_to(pdir(pid)))

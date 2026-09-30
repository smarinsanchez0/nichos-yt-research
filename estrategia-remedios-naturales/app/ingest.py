"""Carga de la foto del avatar y del video original (usado por la API y por el modo automatico)."""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

from . import media, store


def save_avatar(pid: str, src: Path) -> dict:
    try:
        im = Image.open(src).convert("RGB")
    except Exception:
        raise RuntimeError(f"No pude leer la imagen del avatar: {src}")
    im.thumbnail((2048, 2048))
    out = store.path(pid, "avatar", "avatar.jpg")
    im.save(out, "JPEG", quality=95)
    with store.edit(pid) as p:
        p["avatar"] = {"file": store.rel(pid, out), "w": im.width, "h": im.height, "profile": None}
    return p["avatar"]


def save_video(pid: str, src: Path) -> dict:
    src = Path(src)
    out = store.path(pid, "source", f"original{src.suffix.lower() or '.mp4'}")
    shutil.copyfile(src, out)
    info = media.probe(out)
    if not info["has_video"] or info["duration"] <= 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"No pude leer el video: {src}")
    with store.edit(pid) as p:
        p["source"] = {"file": store.rel(pid, out), "name": src.name, **info}
        p["analysis"] = {"points": {"transcription": False, "translation": False, "scenes": False, "prompts": False},
                         "transcript": None, "translation": None}
        p["scenes"], p["final"] = [], None
    return p["source"]

"""Veo 3.1 directo con la key de Google AI Studio (Gemini API): respaldo rapido y estable cuando DubVoice se atasca.
Imagen -> video 9:16 con audio nativo. Duraciones: 4, 6 u 8 s. Pago por uso (sin plan)."""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path

from ..config import require_key
from .http import fail, request

BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "veo-3.1-fast-generate-preview"
APPROX_COST_PER_SECOND = 0.10      # USD, orientativo (Veo 3.1 fast con audio 720p); verifica en tu consola de Google


def _h() -> dict:
    return {"x-goog-api-key": require_key("google"), "content-type": "application/json"}


def pick_duration(seconds: float | None) -> int:
    for d in (4, 6, 8):
        if (seconds or 8) <= d:
            return d
    return 8


def _image_part(path: Path) -> dict:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((1280, 1280))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return {"bytesBase64Encoded": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/jpeg"}


def _find_uri(resp: dict) -> str | None:
    g = resp.get("generateVideoResponse") or resp
    for s in (g.get("generatedSamples") or g.get("videos") or []):
        v = s.get("video") or s
        if v.get("uri"):
            return v["uri"]
    return None


def veo(prompt: str, image_path: Path, model: str = DEFAULT_MODEL, aspect: str = "9:16", duration: float | None = 8,
        resolution: str = "720p", progress=None, timeout: float = 600, cancel=None) -> tuple[str, bytes]:
    dur = pick_duration(duration)
    body = {"instances": [{"prompt": prompt, "image": _image_part(image_path)}],
            "parameters": {"aspectRatio": aspect, "durationSeconds": dur, "resolution": resolution}}
    r = request("POST", f"{BASE}/models/{model}:predictLongRunning", json=body, headers=_h(), timeout=120)
    if r.status_code != 200:
        raise fail("Google Veo", r)
    name = r.json().get("name")
    if not name:
        raise RuntimeError(f"Google Veo no devolvio operacion: {str(r.json())[:200]}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Cancelado")
        time.sleep(8)
        g = request("GET", f"{BASE}/{name}", headers=_h(), timeout=60)
        if g.status_code != 200:
            raise fail("Google Veo (estado)", g)
        j = g.json()
        if j.get("done"):
            if j.get("error"):
                raise RuntimeError(f"Google Veo fallo: {str(j['error'])[:300]}")
            resp = j.get("response") or {}
            uri = _find_uri(resp)
            if not uri:
                reasons = (resp.get("generateVideoResponse") or {}).get("raiMediaFilteredReasons")
                raise RuntimeError(f"Google Veo no entrego video (filtro de seguridad: {reasons or 'sin detalle'})")
            d = request("GET", uri, headers=_h(), timeout=300, follow_redirects=True)
            if d.status_code != 200:
                raise fail("Google Veo (descarga)", d)
            return name.split("/")[-1], d.content
        if progress:
            el = int(time.time() - t0)
            progress(f"Google Veo generando… {el // 60}:{el % 60:02d} min")
    raise RuntimeError(f"Google Veo no termino en {int(timeout // 60)} min")

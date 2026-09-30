"""DubVoice.ai: Nano Banana (imagen) y Veo 3.1 (imagen -> video 9:16) por API, cobro en creditos.

Nota: la documentacion publica no detalla la forma exacta de la respuesta al crear/consultar tareas de
video e imagen, asi que el parseo es tolerante (varios nombres de campo) y el sondeo prueba rutas conocidas.
"""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path

from ..config import require_key
from .http import fail, request
from .kie import download

BASE = "https://www.dubvoice.ai"
DONE = {"completed", "succeeded", "success", "done", "finished"}
FAILED = {"failed", "error", "fail", "cancelled"}

# creditos publicados (docs API) - para el estimador de costo
CREDITS = {"veo-3.1-fast": 7500, "veo-3.1-lite": 9100, "veo-3.1": 17000, "meta": 2000,
           "nano-banana-2-lite": 500, "nano-banana-2": 1000, "nano-banana-pro": 3500, "grok-image": 1000}


def _h() -> dict:
    return {"Authorization": f"Bearer {require_key('dubvoice')}", "Content-Type": "application/json"}


def data_uri(path: Path, max_side: int = 1280) -> str:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _flat(j) -> dict:
    d = dict(j) if isinstance(j, dict) else {}
    if isinstance(d.get("data"), dict):
        d = {**d, **d["data"]}
    return d


def _task_id(d: dict) -> str | None:
    for k in ("task_id", "taskId", "id", "job_id", "jobId", "generation_id"):
        if d.get(k):
            return str(d[k])
    return None


def _status(d: dict) -> str:
    return str(d.get("status") or d.get("state") or "").lower()


def _collect(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v.startswith("http") else []
    if isinstance(v, list):
        return [u for x in v for u in _collect(x)]
    if isinstance(v, dict):
        return [u for x in v.values() for u in _collect(x)]
    return []


def _urls(d: dict) -> list[str]:
    for k in ("image_urls", "image_url", "video_url", "file_url", "result", "url", "output", "video", "urls"):
        if k in d:
            found = _collect(d[k])
            if found:
                return found
    return []


def _check(service: str, r):
    if r.status_code == 402:
        raise RuntimeError(f"{service}: creditos insuficientes en DubVoice. {r.text[:200]}")
    if r.status_code not in (200, 201, 202):
        raise fail(service, r)
    try:
        return _flat(r.json())
    except Exception:
        raise RuntimeError(f"{service}: respuesta no JSON: {r.text[:200]}")


def _wait(get, service: str, timeout: float, interval: float) -> list[str]:
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(interval)
        d = get()
        s = _status(d)
        if s in FAILED:
            raise RuntimeError(f"{service} fallo: {d.get('error') or d.get('message') or 'sin detalle'} (creditos reembolsados por DubVoice)")
        urls = _urls(d)
        if urls and (s in DONE or not s):
            return urls
        if s in DONE:
            raise RuntimeError(f"{service} termino sin URL de resultado: {str(d)[:300]}")
    raise RuntimeError(f"{service} tardo demasiado (timeout)")


def image(prompt: str, refs: list[Path], model: str = "nano-banana-2", aspect: str = "9:16") -> bytes:
    body = {"prompt": prompt, "model": model, "aspect_ratio": aspect}
    if refs:
        body["image_input"] = [data_uri(p) for p in refs][:4]
    r = request("POST", f"{BASE}/api/image-generate", json=body, headers=_h(), timeout=600)
    d = _check("DubVoice (imagen)", r)
    urls = _urls(d)
    if not urls or _status(d) in {"pending", "processing", "queued"}:
        tid = _task_id(d)
        if not tid:
            raise RuntimeError(f"DubVoice (imagen) no devolvio resultado ni id de tarea: {str(d)[:300]}")
        urls = _wait(lambda: _check("DubVoice (imagen)", request(
            "GET", f"{BASE}/api/image-generate/status", params={"id": tid}, headers=_h())),
            "DubVoice (imagen)", 600, 4)
    return download(urls[0])


_video_poll_url: list[tuple[str, str]] = []


def veo(prompt: str, image_path: Path, model: str = "veo-3.1-fast", aspect: str = "9:16",
        resolution: str = "720p", progress=None, timeout: float = 1200) -> tuple[str, bytes]:
    body = {"prompt": prompt, "model": model, "aspect_ratio": aspect, "resolution": resolution,
            "ref_images": [data_uri(image_path, 1600)], "mode_image": "frame"}
    r = request("POST", f"{BASE}/api/v1/video", json=body, headers=_h(), timeout=600)
    d = _check("DubVoice (video)", r)
    tid = _task_id(d)
    urls = _urls(d)
    if urls and _status(d) not in {"pending", "processing", "queued"}:
        return tid or "sync", download(urls[0])
    if not tid:
        raise RuntimeError(f"DubVoice (video) no devolvio id de tarea: {str(d)[:300]}")
    if progress:
        progress(f"Veo (DubVoice) en cola (task {tid[:8]}…)")

    candidates = [("/api/v1/video", "task_id"), ("/api/v1/video", "id"), ("/api/v1/video/status", "task_id")]

    def get():
        for path, key in (_video_poll_url or candidates):
            g = request("GET", f"{BASE}{path}", params={key: tid}, headers=_h())
            if g.status_code == 404:
                continue
            if not _video_poll_url:
                _video_poll_url.append((path, key))
            return _check("DubVoice (video)", g)
        raise RuntimeError("DubVoice: no encontre la ruta para consultar el video (revisa /dashboard/api-docs).")

    urls = _wait(get, "DubVoice (video)", timeout, 8)
    return tid, download(urls[0])

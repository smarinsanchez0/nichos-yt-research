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
OMNI_TIERS = {4: 4688, 6: 6250, 8: 7813, 10: 9375}     # omniflash 720p por duracion (360p cuesta la mitad)
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


def _post(service: str, path: str, body: dict, timeout: float = 600):
    """POST con espera automatica cuando DubVoice limita (429: max 3 en paralelo / 10 por minuto)."""
    for attempt in range(20):
        r = request("POST", f"{BASE}{path}", json=body, headers=_h(), timeout=timeout, retries=1)
        if r.status_code != 429:
            return r
        try:
            wait = float(r.headers.get("Retry-After") or 0)
        except ValueError:
            wait = 0
        time.sleep(max(wait, 8))
    return r


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


def image(prompt: str, refs: list[Path], model: str = "nano-banana-2", aspect: str = "9:16", progress=None) -> bytes:
    body = {"prompt": prompt, "model": model, "aspect_ratio": aspect}
    if refs:
        body["image_input"] = [data_uri(p) for p in refs][:4]
    _, urls = _submit_and_wait("DubVoice (imagen)", "/api/image-generate", body,
                               [("/api/image-generate/status", "id"), ("/api/image-generate/status", "task_id"),
                                ("/api/image-generate/status", "taskId")], 240, 4, progress, post_timeout=120)
    return download(urls[0])


_poll_cache: dict[str, tuple[str, str]] = {}


def _submit_and_wait(service: str, path: str, body: dict, poll_candidates: list[tuple[str, str]],
                     timeout: float, interval: float, progress=None, post_timeout: float = 600) -> tuple[str, list[str]]:
    d = _check(service, _post(service, path, body, post_timeout))
    tid = _task_id(d)
    urls = _urls(d)
    if urls and _status(d) not in {"pending", "processing", "queued"}:
        return tid or "sync", urls
    if not tid:
        raise RuntimeError(f"{service} no devolvio id de tarea: {str(d)[:300]}")
    if progress:
        progress(f"{service} en cola (task {tid[:8]}…)")

    def get():
        for p_, key in ([_poll_cache[path]] if path in _poll_cache else poll_candidates):
            g = request("GET", f"{BASE}{p_}", params={key: tid}, headers=_h())
            if g.status_code == 404:
                continue
            _poll_cache[path] = (p_, key)
            return _check(service, g)
        raise RuntimeError(f"{service}: no encontre la ruta para consultar la tarea (revisa /dashboard/api-docs).")

    return tid, _wait(get, service, timeout, interval)


def tier_for(seconds: float, model: str = "omniflash") -> int:
    """Duracion que se pide: la menor disponible que cubre `seconds`. Veo siempre genera 8 s; Omni Flash 4/6/8/10."""
    if model != "omniflash":
        return 8
    for t in sorted(OMNI_TIERS):
        if seconds <= t:
            return t
    return 10


def credits_for(model: str, seconds: float) -> int:
    if model == "omniflash":
        return OMNI_TIERS[tier_for(seconds, model)]
    return CREDITS.get(model, 7500)


def veo(prompt: str, image_path: Path, model: str = "veo-3.1-fast", aspect: str = "9:16",
        resolution: str = "720p", progress=None, timeout: float = 1200, duration: float | None = None) -> tuple[str, bytes]:
    body = {"prompt": prompt, "model": model, "aspect_ratio": aspect, "resolution": resolution,
            "ref_images": [data_uri(image_path, 1600)], "mode_image": "frame"}
    if model == "omniflash":
        body["duration"] = tier_for(duration or 8, model)
    tid, urls = _submit_and_wait("DubVoice (video)", "/api/v1/video", body,
                                 [("/api/v1/video", "task_id"), ("/api/v1/video", "id"), ("/api/v1/video/status", "task_id")],
                                 timeout, 8, progress)
    return tid, download(urls[0])


# ------------------------------------------------------------------ voces
def list_voices(gender: str | None = None, language: str = "en", n: int = 40) -> list[dict]:
    params = {"provider": "elevenlabs", "page_size": n}
    if language:
        params["language"] = language
    if gender in ("male", "female"):
        params["gender"] = gender
    r = request("GET", f"{BASE}/api/v1/voices", params=params, headers=_h(), timeout=60)
    if r.status_code != 200:
        raise fail("DubVoice (voces)", r)
    out = []
    for v in r.json().get("voices", []):
        out.append({"voice_id": v.get("voice_id"), "name": v.get("name"), "category": "dubvoice",
                    "gender": (v.get("gender") or "").lower(), "age": (v.get("age") or "").lower(),
                    "accent": (v.get("accent") or "").lower(), "descriptive": (v.get("description") or v.get("descriptive") or "").lower(),
                    "use_case": (v.get("use_case") or "").lower(), "preview_url": v.get("preview_url")})
    return out


def voice_change(audio_url: str, voice_id: str, progress=None) -> bytes:
    """Cambia la voz de un audio (URL publica) a `voice_id` conservando tiempos (2.000 creditos/min)."""
    tid, urls = _submit_and_wait("DubVoice (cambio de voz)", "/api/v1/voice-changer",
                                 {"audio_url": audio_url, "target_voice_id": voice_id},
                                 [("/api/v1/voice-changer", "task_id"), ("/api/v1/voice-changer", "id")], 600, 5, progress)
    return download(urls[0])

"""Veo 3.1 directo con la key de Google AI Studio (Gemini API): respaldo rapido y estable cuando DubVoice se atasca.
Imagen -> video 9:16 con audio nativo. Duraciones: 4, 6 u 8 s. Pago por uso (sin plan)."""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path

from ..config import require_key
from . import errors
from .errors import Cancelled, ErrorType, F5Error
from .http import download as http_download, fail, request, request_once, typed_error

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


DEFAULT_LIMITS = {"submission": 90, "poll_request": 20, "processing": 720, "download": 180, "download_retries": 3, "poll_interval": 8.0,
                  "max_poll_failures": 8}


def _sleep(seconds: float, cancel) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        time.sleep(min(0.25, max(end - time.time(), 0.01)))


def veo(prompt: str, image_path: Path, model: str = DEFAULT_MODEL, aspect: str = "9:16", duration: float | None = 8,
        resolution: str = "720p", progress=None, timeout: float = 600, cancel=None, on_submit=None, on_poll=None,
        limits: dict | None = None, gate=None, recorder=None) -> tuple[str, bytes]:
    """Sin `limits`: comportamiento heredado. Con `limits` (Fase 5): timeouts reales, cero reintentos internos, errores tipados y el
    nombre de la operacion entregado en `on_submit` en cuanto existe (permite reconciliar con `resume`)."""
    if limits is None:
        return _veo_legacy(prompt, image_path, model, aspect, duration, resolution, progress, timeout, cancel)
    lim = {**DEFAULT_LIMITS, **limits}
    dur = pick_duration(duration)
    body = {"instances": [{"prompt": prompt, "image": _image_part(image_path)}],
            "parameters": {"aspectRatio": aspect, "durationSeconds": dur, "resolution": resolution}}
    url = f"{BASE}/models/{model}:predictLongRunning"
    if gate:
        gate()
    t0 = time.time()
    try:
        r = request_once("POST", url, json=body, headers=_h(), connect=10, read=min(60, lim["submission"]), deadline=lim["submission"], cancel=cancel)
    except F5Error as e:
        e.provider = "google"
        if recorder:
            recorder.http("submit", "POST", url, {"durationSeconds": dur, "model": model}, error=f"{e.etype.value} ambiguous={e.ambiguous} {e}", t0=t0)
        raise
    if recorder:
        recorder.http("submit", "POST", url, {"durationSeconds": dur, "model": model}, resp=r, t0=t0)
    if r.status_code != 200:
        err = typed_error("Google Veo", r, "google")
        if r.status_code in (502, 504):
            err.ambiguous = True
        raise err
    try:
        name = r.json().get("name")
    except Exception:  # noqa: BLE001
        name = None
    if not name:
        raise F5Error(ErrorType.INVALID_RESPONSE, "Google Veo no devolvio operacion", provider="google", ambiguous=True, sub="no_job_id")
    if on_submit:
        try:
            on_submit(name, {"resolved_by": "name"})
        except Exception:  # noqa: BLE001
            pass
    return name, _poll_and_download(name, lim, progress, cancel, on_poll, gate, recorder, time.time())


def resume(job_id: str, model: str | None = None, progress=None, cancel=None, on_poll=None, limits: dict | None = None, gate=None,
           recorder=None) -> tuple[str, bytes]:
    """RECONCILIACION por operation ID: consulta la operacion existente y descarga el video. Nunca crea otra generacion."""
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    return job_id, _poll_and_download(job_id, lim, progress, cancel, on_poll, gate, recorder, time.time())


def _poll_and_download(name: str, lim: dict, progress, cancel, on_poll, gate, rec, started: float) -> bytes:
    deadline = started + lim["processing"]
    fails = polls = 0
    last = ""
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        if time.time() > deadline:
            raise F5Error(ErrorType.PROVIDER_TIMEOUT, f"Google Veo no termino en {int(lim['processing'] // 60)} min. Ultimo estado: {last}",
                          provider="google", job_id=name)
        _sleep(lim["poll_interval"], cancel)
        if gate:
            gate()
        t0 = time.time()
        try:
            g = request_once("GET", f"{BASE}/{name}", headers=_h(), connect=10, read=lim["poll_request"], deadline=lim["poll_request"], cancel=cancel)
        except F5Error as e:
            fails += 1
            last = f"{e.etype.value}: {str(e)[:120]}"
            if rec:
                rec.http("poll", "GET", f"{BASE}/{name}", None, error=last, t0=t0, n=polls + 1)
            if fails >= lim["max_poll_failures"]:
                raise F5Error(ErrorType.CONNECTION_ERROR, f"Google Veo: {fails} sondeos seguidos fallaron ({last})", provider="google", job_id=name,
                              sub="poll_failures")
            continue
        if rec:
            rec.http("poll", "GET", f"{BASE}/{name}", None, resp=g, t0=t0, n=polls + 1)
        if g.status_code != 200:
            err = typed_error("Google Veo (estado)", g, "google")
            err.job_id = name
            if g.status_code == 404:
                err.etype, err.sub = ErrorType.INVALID_RESPONSE, "operation_not_found"
                raise err
            if err.fatal:
                raise err
            fails += 1
            last = f"{err.etype.value}: {str(err)[:120]}"
            if err.etype == ErrorType.RATE_LIMIT and err.retry_after:
                _sleep(min(err.retry_after, 60), cancel)
            if fails >= lim["max_poll_failures"]:
                raise F5Error(ErrorType.CONNECTION_ERROR, f"Google Veo: {fails} sondeos seguidos fallaron ({last})", provider="google", job_id=name,
                              sub="poll_failures")
            continue
        fails = 0
        polls += 1
        try:
            j = g.json()
        except Exception:  # noqa: BLE001
            raise F5Error(ErrorType.INVALID_RESPONSE, "Google Veo (estado): respuesta no JSON", provider="google", job_id=name, sub="poll_not_json")
        if on_poll:
            try:
                on_poll(polls, "done" if j.get("done") else "processing")
            except Exception:  # noqa: BLE001
                pass
        if j.get("done"):
            if j.get("error"):
                msg = f"Google Veo fallo: {str(j['error'])[:300]}"
                et = errors.classify(msg)
                et = et if et in (ErrorType.CONTENT_FILTER, ErrorType.PROVIDER_REJECTED) else ErrorType.PROVIDER_REJECTED
                raise F5Error(et, msg, provider="google", job_id=name, sub=errors.sub_of(msg) or "provider_failed", fatal=False)
            resp = j.get("response") or {}
            uri = _find_uri(resp)
            if not uri:
                reasons = (resp.get("generateVideoResponse") or {}).get("raiMediaFilteredReasons")
                raise F5Error(ErrorType.CONTENT_FILTER, f"Google Veo no entrego video (filtro de seguridad: {reasons or 'sin detalle'})",
                              provider="google", job_id=name, sub="provider_failed")
            return _download(uri, name, lim, cancel, rec)
        if progress:
            el = int(time.time() - started)
            progress(f"Google Veo generando… {el // 60}:{el % 60:02d} min")


def _download(uri: str, name: str, lim: dict, cancel, rec) -> bytes:
    last = None
    for i in range(max(int(lim["download_retries"]), 1)):
        if i:
            _sleep(2 * i, cancel)
        try:
            data = http_download(uri, headers={"x-goog-api-key": require_key("google")}, deadline=lim["download"], cancel=cancel)
            if rec:
                rec.note("download", bytes=len(data), attempt=i + 1) if hasattr(rec, "note") else None
            return data
        except F5Error as e:
            last = e
    raise F5Error(ErrorType.DOWNLOAD_ERROR, f"Google Veo: no se pudo descargar el video ({last})", provider="google", job_id=name)


def _veo_legacy(prompt: str, image_path: Path, model: str = DEFAULT_MODEL, aspect: str = "9:16", duration: float | None = 8,
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

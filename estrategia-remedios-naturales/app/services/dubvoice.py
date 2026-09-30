"""DubVoice.ai: Nano Banana (imagen) y Veo 3.1 (imagen -> video 9:16) por API, cobro en creditos.

Nota: la documentacion publica no detalla la forma exacta de la respuesta al crear/consultar tareas de
video e imagen, asi que el parseo es tolerante (varios nombres de campo) y el sondeo prueba rutas conocidas.
"""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path

from .. import store
from ..config import DATA_DIR, require_key
from . import errors
from .errors import Cancelled as _Cancelled, ErrorType, F5Error
from .http import download as http_download, fail, request, request_once, typed_error
from .kie import download

BASE = "https://www.dubvoice.ai"
DONE = {"completed", "succeeded", "success", "done", "finished"}
FAILED = {"failed", "error", "fail", "cancelled"}

# creditos publicados (docs API) - para el estimador de costo
OMNI_TIERS = {4: 4688, 6: 6250, 8: 7813, 10: 9375}     # omniflash 720p por duracion (360p cuesta la mitad)
CREDITS = {"veo-3.1-fast": 7500, "veo-3.1-lite": 9100, "veo-3.1": 17000, "meta": 2000,
           "nano-banana-2-lite": 500, "nano-banana-2": 1000, "nano-banana-pro": 3500, "grok-image": 1000}


def _h(auth: str = "bearer", json_body: bool = True) -> dict:
    key = require_key("dubvoice")
    h = {"Authorization": f"Bearer {key}"} if auth == "bearer" else {"X-API-Key": key}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


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


import threading

_throttle_lock = threading.Lock()
_last_post = [0.0]
MIN_GAP = 6.5          # DubVoice: 10 solicitudes/min por key


def _throttle(cancel=None) -> None:
    with _throttle_lock:
        wait = MIN_GAP - (time.time() - _last_post[0])
        if wait > 0:
            time.sleep(wait)
        _last_post[0] = time.time()


Cancelled = _Cancelled          # una sola clase de cancelacion para toda la Fase 5


def _post(service: str, path: str, body: dict, timeout: float = 600, auth: str = "bearer", cancel=None):
    """POST con espera automatica cuando DubVoice limita (429: max 3 en paralelo / 10 por minuto)."""
    net_errors = 0
    for attempt in range(24):
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        _throttle()
        try:
            r = request("POST", f"{BASE}{path}", json=body, headers=_h(auth), timeout=timeout, retries=1)
        except RuntimeError as e:          # corte de conexion (p. ej. 'Connection reset by peer'): reintentar con espera
            net_errors += 1
            if net_errors >= 4 or "Sin conexion" not in str(e):
                raise
            time.sleep(5 * net_errors)
            continue
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


def _wait(get, service: str, timeout: float, interval: float, progress=None, cancel=None) -> list[str]:
    t0 = time.time()
    last, errors = "", 0
    while time.time() - t0 < timeout:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        time.sleep(interval)
        try:
            d = get()
            errors = 0
        except RuntimeError as e:      # fallo puntual de red/API: se tolera un par de veces
            errors += 1
            last = str(e)[:250]
            if errors >= 4:
                raise
            continue
        s = _status(d)
        last = str({k: d.get(k) for k in ("status", "state", "progress", "error", "message") if k in d})[:300]
        if s in FAILED:
            raise RuntimeError(f"{service} fallo: {d.get('error') or d.get('message') or 'sin detalle'} (creditos reembolsados por DubVoice)")
        urls = _urls(d)
        if urls and (s in DONE or not s):
            return urls
        if s in DONE:
            raise RuntimeError(f"{service} termino sin URL de resultado: {str(d)[:300]}")
        if progress:
            el = int(time.time() - t0)
            progress(f"{service} generando… {el // 60}:{el % 60:02d} min (estado: {s or 'en proceso'})")
    raise RuntimeError(f"{service} no termino en {int(timeout // 60)} min. Ultimo estado: {last}")


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
                     timeout: float, interval: float, progress=None, post_timeout: float = 600,
                     auth: str = "bearer", cancel=None) -> tuple[str, list[str]]:
    d = _check(service, _post(service, path, body, post_timeout, auth, cancel))
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
            g = request("GET", f"{BASE}{p_}", params={key: tid}, headers=_h(auth))
            if g.status_code == 404:
                continue
            _poll_cache[path] = (p_, key)
            return _check(service, g)
        raise RuntimeError(f"{service}: no encontre la ruta para consultar la tarea (revisa /dashboard/api-docs).")

    return tid, _wait(get, service, timeout, interval, progress, cancel)


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
        resolution: str = "720p", progress=None, timeout: float = 600, duration: float | None = None,
        cancel=None, on_submit=None, on_poll=None, limits: dict | None = None, gate=None, recorder=None) -> tuple[str, bytes]:
    """Imagen → video. Sin `limits` se comporta como siempre (camino heredado/manual). Con `limits` (Fase 5) usa el camino ESTRICTO:
    timeouts reales por fase, cero reintentos internos, errores tipados, job_id entregado en `on_submit` en cuanto existe y
    registro sanitizado del contrato en `recorder`."""
    body = {"prompt": prompt, "model": model, "aspect_ratio": aspect, "resolution": resolution,
            "ref_images": [data_uri(image_path, 1280)], "mode_image": "frame"}
    if model == "omniflash":
        body["duration"] = tier_for(duration or 8, model)
    if limits is None:
        tid, urls = _submit_and_wait("DubVoice (video)", "/api/v1/video", body,
                                     [("/api/v1/video", "task_id"), ("/api/v1/video", "id"), ("/api/v1/video/status", "task_id")],
                                     timeout, 8, progress, cancel=cancel)
        return tid, download(urls[0])
    return _veo_strict(body, progress, cancel, on_submit, on_poll, limits, gate, recorder)


# ==================================================================== Fase 5: camino estricto + contrato observable
PENDING_STATES = {"pending", "processing", "queued", "running", "in_progress", "in-progress", "generating", "waiting", "submitted", "created",
                  "starting", "started", "active"}
CANDIDATES = [("/api/v1/video", "task_id"), ("/api/v1/video", "id"), ("/api/v1/video/status", "task_id")]
DEFAULT_LIMITS = {"submission": 90, "poll_request": 20, "processing": 720, "download": 180, "download_retries": 3, "poll_interval": 8.0,
                  "max_poll_failures": 8}
_ID_KEYS = ("task_id", "taskId", "id", "job_id", "jobId", "generation_id")
_KEEP_HEADERS = ("content-type", "retry-after", "x-request-id", "date")


def contract_file() -> Path:
    return DATA_DIR / "contracts" / "dubvoice.json"


def contract_verified() -> bool:
    """¿Ya se observo un ciclo real completo (POST → job_id → sondeo → estados → URL → descarga)?"""
    try:
        import json
        return bool(json.loads(contract_file().read_text()).get("verified"))
    except Exception:  # noqa: BLE001
        return False


class ContractRecorder:
    """Registra, SANITIZADO, lo que DubVoice realmente responde (status HTTP, forma del JSON, job_id, endpoint de sondeo, estados reales,
    URL final sin firma, tiempos) en data/projects/<id>/f5_contract.jsonl. Nunca escribe claves, headers de peticion, tokens ni base64."""

    def __init__(self, pid: str, provider: str = "dubvoice", si: int = 0, ci: int = 0, att_id: str = "?", on_verified=None):
        self.pid, self.provider, self.si, self.ci, self.att = pid, provider, si, ci, att_id
        self.on_verified = on_verified
        self.t0 = time.time()
        self.statuses: list[tuple[float, str]] = []
        self.info: dict = {}
        self._unknown: set[str] = set()

    def _write(self, rec: dict) -> None:
        try:
            import json
            base = {"ts": round(time.time(), 3), "provider": self.provider, "scene": self.si, "clip": self.ci, "attempt": self.att,
                    "t_rel": round(time.time() - self.t0, 2)}
            with open(store.path(self.pid, "f5_contract.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(errors.sanitize({**base, **rec}), ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def http(self, op: str, method: str, url: str, req_shape=None, resp=None, error: str | None = None, t0: float | None = None, **extra) -> None:
        rec = {"op": op, "method": method, "url": errors.scrub(url, 300), "request_shape": req_shape,
               "elapsed_ms": int((time.time() - t0) * 1000) if t0 else None}
        rec.update(extra)
        if error:
            rec["error"] = errors.scrub(error, 300)
        if resp is not None:
            rec["http_status"] = resp.status_code
            rec["headers"] = {k: v for k, v in resp.headers.items()
                              if k.lower() in _KEEP_HEADERS or k.lower().startswith(("x-ratelimit", "ratelimit", "x-rate"))}
            try:
                j = resp.json()
                rec["json_shape"] = errors.shape(j)
                rec["body"] = j
            except Exception:  # noqa: BLE001
                rec["body_text"] = (getattr(resp, "text", "") or "")[:300]
        self._write(rec)

    def note(self, kind: str, **data) -> None:
        self._write({"op": kind, **data})

    def status_seen(self, job_id: str, raw: str, normalized: str) -> None:
        if not self.statuses or self.statuses[-1][1] != raw:
            self.statuses.append((round(time.time() - self.t0, 1), raw))
            self._write({"op": "status_transition", "job_id": job_id, "raw_status": raw, "normalized": normalized})
        if normalized == "unrecognized" and raw not in self._unknown:
            self._unknown.add(raw)
            self._write({"op": "UNRECOGNIZED_STATUS", "job_id": job_id, "raw_status": raw})

    def verified(self, **info) -> None:
        """Ciclo completo observado: se guarda el contrato para abrir la concurrencia."""
        try:
            import json
            f = contract_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            rec = {"verified": True, "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"), "statuses_seen": [s for _, s in self.statuses],
                   **self.info, **info}
            f.write_text(json.dumps(errors.sanitize(rec), ensure_ascii=False, indent=1))
            self._write({"op": "CONTRACT_VERIFIED", **rec})
            if self.on_verified:
                self.on_verified(rec)
        except Exception:  # noqa: BLE001
            pass


class _NullRecorder:
    info: dict = {}

    def http(self, *a, **k): pass
    def note(self, *a, **k): pass
    def status_seen(self, *a, **k): pass
    def verified(self, *a, **k): pass


def _sleep(seconds: float, cancel) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        time.sleep(min(0.25, max(end - time.time(), 0.01)))


def _id_and_key(j) -> tuple[str | None, str | None]:
    """job_id y el NOMBRE del campo por el que se resolvio (top-level o dentro de 'data')."""
    if not isinstance(j, dict):
        return None, None
    for k in _ID_KEYS:
        if j.get(k):
            return str(j[k]), k
    if isinstance(j.get("data"), dict):
        for k in _ID_KEYS:
            if j["data"].get(k):
                return str(j["data"][k]), f"data.{k}"
    return None, None


def _veo_strict(body: dict, progress, cancel, on_submit, on_poll, limits, gate, recorder) -> tuple[str, bytes]:
    lim = {**DEFAULT_LIMITS, **limits}
    rec = recorder or _NullRecorder()
    h = _h()
    url = f"{BASE}/api/v1/video"
    shape = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 80 else ("<list>" if isinstance(v, list) else v)) for k, v in body.items()}
    # ---- 1) ENVIO (un solo POST, sin reintentos internos)
    if gate:
        gate()
    t0 = time.time()
    try:
        r = request_once("POST", url, json=body, headers=h, connect=10, read=min(60, lim["submission"]), deadline=lim["submission"], cancel=cancel)
    except F5Error as e:
        e.provider = "dubvoice"
        rec.http("submit", "POST", url, shape, error=f"{e.etype.value} ambiguous={e.ambiguous} {e}", t0=t0)
        raise
    rec.http("submit", "POST", url, shape, resp=r, t0=t0)
    if r.status_code not in (200, 201, 202):
        err = typed_error("DubVoice (video)", r, "dubvoice")
        if r.status_code in (502, 504):          # el proxy pudo cortar DESPUES de crear el job
            err.ambiguous = True
        raise err
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        raise F5Error(ErrorType.INVALID_RESPONSE, "DubVoice (video): la respuesta 2xx no es JSON", provider="dubvoice", ambiguous=True, sub="not_json")
    d = _flat(j)
    tid, id_field = _id_and_key(j)
    urls = _urls(d)
    if not tid:
        if urls and _status(d) in (DONE | {""}):
            rec.note("submit_sync_result", note="respuesta sincrona con URL y sin job_id")
            return "sync", _download_strict(urls, None, lim, gate, cancel, rec, lambda: [])
        raise F5Error(ErrorType.INVALID_RESPONSE, f"DubVoice (video) no devolvio id de tarea. Forma: {errors.shape(j)}", provider="dubvoice",
                      ambiguous=True, sub="no_job_id")
    rec.info.update(submit_id_field=id_field, submit_response_shape=errors.shape(j))
    if on_submit:
        try:
            on_submit(tid, {"resolved_by": id_field})
        except Exception:  # noqa: BLE001
            pass
    if progress:
        progress(f"DubVoice (video) en cola (task {tid[:8]}…)")
    return tid, _wait_and_download(tid, d, lim, h, progress, cancel, on_poll, gate, rec, submitted_at=time.time())


def _poll_once(tid: str, h: dict, lim: dict, cancel, rec, n: int) -> dict:
    """Un sondeo. Prueba las rutas candidatas (404 → siguiente) y recuerda la que responde."""
    cands = [_poll_cache["/api/v1/video"]] if "/api/v1/video" in _poll_cache else CANDIDATES
    for p_, key in cands:
        t0 = time.time()
        try:
            g = request_once("GET", f"{BASE}{p_}", params={key: tid}, headers=h, connect=10, read=lim["poll_request"],
                             deadline=lim["poll_request"], cancel=cancel)
        except F5Error as e:
            rec.http("poll", "GET", f"{BASE}{p_}", {"param": key}, error=f"{e.etype.value} {e}", t0=t0, n=n)
            raise
        rec.http("poll", "GET", f"{BASE}{p_}", {"param": key}, resp=g, t0=t0, n=n)
        if g.status_code == 404:
            _poll_cache.pop("/api/v1/video", None)
            continue
        if g.status_code != 200:
            raise typed_error("DubVoice (video, estado)", g, "dubvoice")
        try:
            j = g.json()
        except Exception:  # noqa: BLE001
            raise F5Error(ErrorType.INVALID_RESPONSE, "DubVoice (video, estado): respuesta no JSON", provider="dubvoice", job_id=tid, sub="poll_not_json")
        _poll_cache["/api/v1/video"] = (p_, key)
        rec.info.update(poll_endpoint=p_, poll_param=key, poll_response_shape=errors.shape(j))
        return _flat(j)
    raise F5Error(ErrorType.INVALID_RESPONSE, "DubVoice (video): ninguna ruta de sondeo conocida responde para este job", provider="dubvoice",
                  job_id=tid, sub="poll_endpoint")


def _wait_and_download(tid: str, first: dict | None, lim: dict, h: dict, progress, cancel, on_poll, gate, rec, submitted_at: float) -> bytes:
    deadline = submitted_at + lim["processing"]
    fails = polls = 0
    last = ""
    d = first
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        if d is None:
            if time.time() > deadline:
                raise F5Error(ErrorType.PROVIDER_TIMEOUT, f"DubVoice (video) no termino en {int(lim['processing'] // 60)} min. Ultimo estado: {last}",
                              provider="dubvoice", job_id=tid)
            _sleep(lim["poll_interval"], cancel)
            if gate:
                gate()
            try:
                d = _poll_once(tid, h, lim, cancel, rec, polls + 1)
                fails = 0
            except F5Error as e:
                if e.etype == ErrorType.INVALID_RESPONSE or (e.etype == ErrorType.PROVIDER_REJECTED and e.fatal):
                    e.job_id = e.job_id or tid
                    raise
                fails += 1
                last = f"{e.etype.value}: {str(e)[:120]}"
                if e.etype == ErrorType.RATE_LIMIT and e.retry_after:
                    _sleep(min(e.retry_after, 60), cancel)
                if fails >= lim["max_poll_failures"]:
                    raise F5Error(ErrorType.CONNECTION_ERROR, f"DubVoice (video): {fails} sondeos seguidos fallaron ({last})", provider="dubvoice",
                                  job_id=tid, sub="poll_failures")
                continue
            polls += 1
        s = _status(d)
        norm = "done" if s in DONE else "failed" if s in FAILED else "processing" if (s in PENDING_STATES or not s) else "unrecognized"
        if s or polls > 0:                      # la respuesta del POST solo cuenta como estado si trae uno
            rec.status_seen(tid, s or "<sin estado>", norm)
        if on_poll and polls > 0:
            try:
                on_poll(polls, s or None)
            except Exception:  # noqa: BLE001
                pass
        last = str({k: d.get(k) for k in ("status", "state", "progress", "error", "message") if k in d})[:300]
        if norm == "failed":
            msg = f"DubVoice (video) fallo: {d.get('error') or d.get('message') or 'sin detalle'} (creditos reembolsados por DubVoice)"
            et = errors.classify(msg)
            et = et if et == ErrorType.CONTENT_FILTER else ErrorType.PROVIDER_REJECTED
            raise F5Error(et, msg, provider="dubvoice", job_id=tid, sub="provider_failed")
        urls = _urls(d)
        if urls and (norm == "done" or not s):
            rec.info.update(status_field="status" if "status" in d else "state" if "state" in d else None,
                            result_field=[k for k in ("video_url", "url", "result", "output", "video", "file_url") if k in d][:1])
            data = _download_strict(urls, tid, lim, gate, cancel, rec, lambda: _refresh_urls(tid, h, lim, cancel, rec))
            rec.verified(result_url_host=errors.scrub(urls[0], 120).split("?")[0], job_id_seen=True)
            return data
        if norm == "done":
            raise F5Error(ErrorType.INVALID_RESPONSE, f"DubVoice (video) termino sin URL de resultado. Forma: {errors.shape(d)}", provider="dubvoice",
                          job_id=tid, sub="done_without_url")
        if progress:
            el = int(time.time() - submitted_at)
            progress(f"DubVoice (video) generando… {el // 60}:{el % 60:02d} min (estado: {s or 'en proceso'})")
        d = None


def _refresh_urls(tid, h, lim, cancel, rec) -> list[str]:
    try:
        return _urls(_poll_once(tid, h, lim, cancel, rec, -1))
    except Exception:  # noqa: BLE001
        return []


def _download_strict(urls: list[str], tid, lim: dict, gate, cancel, rec, refresh) -> bytes:
    last: F5Error | None = None
    for i in range(max(int(lim["download_retries"]), 1)):
        if i > 0:
            _sleep(2 * i, cancel)
            fresh = refresh()
            if fresh:
                urls = fresh
        t0 = time.time()
        try:
            data = http_download(urls[0], deadline=lim["download"], cancel=cancel)
            rec.note("download", url=errors.scrub(urls[0], 200).split("?")[0], bytes=len(data), elapsed_ms=int((time.time() - t0) * 1000), attempt=i + 1)
            return data
        except F5Error as e:
            last = e
            rec.note("download_error", error=str(e)[:200], attempt=i + 1)
    raise F5Error(ErrorType.DOWNLOAD_ERROR, f"No se pudo descargar el video ({last})", provider="dubvoice", job_id=tid, sub=(last.sub if last else None),
                  result_url=errors.scrub(urls[0], 200).split("?")[0])


def resume(job_id: str, model: str | None = None, progress=None, cancel=None, on_poll=None, limits: dict | None = None, gate=None,
           recorder=None) -> tuple[str, bytes]:
    """RECONCILIACION: sondea un job YA existente y descarga su resultado. Jamas crea un job nuevo (cero POST de creacion)."""
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    rec = recorder or _NullRecorder()
    rec.note("resume", job_id=job_id)
    return job_id, _wait_and_download(job_id, None, lim, _h(), progress, cancel, on_poll, gate, rec, submitted_at=time.time())


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


_voice_mode: list[str] = []       # forma de llamada que funciono (se recuerda para los siguientes clips)


def _voice_via_json(audio_url: str, voice_id: str, auth: str, progress=None) -> bytes:
    tid, urls = _submit_and_wait("DubVoice (cambio de voz)", "/api/v1/voice-changer",
                                 {"audio_url": audio_url, "target_voice_id": voice_id},
                                 [("/api/v1/voice-changer", "task_id"), ("/api/v1/voice-changer", "id")], 600, 5, progress, auth=auth)
    return download(urls[0])


def _voice_via_upload(audio_path: Path, voice_id: str, auth: str, progress=None) -> bytes:
    """Ruta antigua con archivo directo (multipart). Puede devolver el audio o un JSON con URL/tarea."""
    with open(audio_path, "rb") as fh:
        r = request("POST", f"{BASE}/api/voice-changer", headers=_h(auth, json_body=False), timeout=600, retries=1,
                    data={"target_voice_id": voice_id, "voice_id": voice_id}, files={"file": (audio_path.name, fh.read(), "audio/mpeg")})
    if r.status_code not in (200, 201, 202):
        raise fail("DubVoice (cambio de voz, subida directa)", r)
    if (r.headers.get("content-type") or "").startswith("audio/") or r.content[:3] == b"ID3":
        return r.content
    d = _flat(r.json())
    urls = _urls(d)
    if urls:
        return download(urls[0])
    tid = _task_id(d)
    if not tid:
        raise RuntimeError(f"DubVoice (cambio de voz) respuesta inesperada: {str(d)[:200]}")
    urls = _wait(lambda: _check("DubVoice (cambio de voz)", request(
        "GET", f"{BASE}/api/voice-changer", params={"task_id": tid}, headers=_h(auth))), "DubVoice (cambio de voz)", 600, 5)
    return download(urls[0])


def voice_change(audio_url: str | None, voice_id: str, progress=None, audio_path: Path | None = None) -> bytes:
    """Cambia la voz a `voice_id` conservando tiempos. Prueba varias formas de llamada y recuerda la que funcione."""
    attempts = [("json-bearer", lambda: _voice_via_json(audio_url, voice_id, "bearer", progress)),
                ("json-xapikey", lambda: _voice_via_json(audio_url, voice_id, "xapikey", progress)),
                ("upload-bearer", lambda: _voice_via_upload(audio_path, voice_id, "bearer", progress)),
                ("upload-xapikey", lambda: _voice_via_upload(audio_path, voice_id, "xapikey", progress))]
    if not audio_url:
        attempts = [a for a in attempts if a[0].startswith("upload")]
    if not audio_path:
        attempts = [a for a in attempts if a[0].startswith("json")]
    if _voice_mode:
        attempts = [a for a in attempts if a[0] == _voice_mode[0]] + [a for a in attempts if a[0] != _voice_mode[0]]
    errs = []
    for name, fn in attempts:
        try:
            out = fn()
            _voice_mode[:] = [name]
            return out
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {str(e)[:160]}")
    raise RuntimeError(" || ".join(errs))


# ------------------------------------------------------------------ locucion (TTS) para clips de respaldo
def tts(text: str, voice_id: str, language: str = "auto", progress=None) -> bytes:
    """Texto -> audio con una voz del catalogo (1 credito por caracter). Endpoint documentado: POST /api/v1/tts + sondeo."""
    body = {"text": text, "voice_id": voice_id, "language": language, "model_id": "eleven_multilingual_v2"}
    _, urls = _submit_and_wait("DubVoice (locucion)", "/api/v1/tts", body, [("/api/v1/tts", "task_id")], 300, 3, progress,
                               post_timeout=60)
    return download(urls[0])


def edge_tts(text: str, voice: str = "es-MX-JorgeNeural") -> bytes:
    """Voz gratuita de Edge: devuelve el MP3 directamente (sin sondeo)."""
    r = request("POST", f"{BASE}/api/edge-tts", json={"text": text, "voice": voice}, headers=_h(), timeout=120)
    if r.status_code != 200:
        raise fail("DubVoice (edge-tts)", r)
    return r.content

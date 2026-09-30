"""FASE 5 · nucleo de trabajos de video.

Reemplaza el "Claude decide todo" por un motor determinista:

  * maquina de estados por clip (PENDING → SUBMITTED → PROCESSING → VALIDATING → ACCEPTED, con RETRY_PENDING / FAILED / NEEDS_REVIEW)
  * historial de intentos persistido (job_id guardado en cuanto existe, prompt, tiempos, errores tipados, creditos)
  * politica de reintentos por TIPO de error (services/errors.py) y proveedor primario → fallback
  * UNA sola autoridad de concurrencia: slots de generacion (SUBMIT → POLL → DESCARGA DEL RAW), limite de peticiones por
    minuto, cooldown ante 429 y ejecutores aparte para voz/unify/auditoria/Claude (que NO ocupan slot)
  * timeouts reales y llamadas acotadas (un worker nunca queda secuestrado)
  * reconciliacion al reabrir la app (jamas se paga otra generacion sin saber que paso con la anterior)

Claude solo hace QC visual y, cuando hace falta, reescribe un prompt (ver supervisor.py). Nunca decide timeouts, limites ni fallback.
El campo `clip.f5` es ADITIVO: los campos heredados (status, file, error, duration, provider_used, ...) se siguen actualizando (`mirror`).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from .. import media, store
from ..config import DATA_DIR, env as _env
from ..services import errors
from ..services.errors import Cancelled, ErrorType, F5Error

# ===================================================================== configuracion (UN solo lugar)
_SPEC = [   # atributo, VARIABLE_DE_ENTORNO, valor por defecto
    ("primary_provider", "F5_PRIMARY_PROVIDER", "dubvoice"),
    ("fallback_provider", "F5_FALLBACK_PROVIDER", "google"),           # "" o "none" = sin fallback
    ("max_concurrent_video_jobs", "MAX_CONCURRENT_VIDEO_JOBS", 3),
    ("video_requests_per_minute", "VIDEO_REQUESTS_PER_MINUTE", 6),     # DubVoice publica 10/min: se deja margen (envios + sondeos)
    ("max_attempts_per_clip", "MAX_ATTEMPTS_PER_CLIP", 3),             # generaciones PAGADAS por clip y ronda
    ("max_cost_per_clip", "MAX_COST_PER_CLIP", 25000),                 # creditos
    ("max_total_generation_time", "MAX_TOTAL_GENERATION_TIME", 1200),  # segundos por clip (20 min)
    ("submission_timeout", "SUBMISSION_TIMEOUT", 90),
    ("poll_request_timeout", "POLL_REQUEST_TIMEOUT", 20),
    ("processing_timeout", "PROCESSING_TIMEOUT", 720),                 # 12 min: hipotesis inicial, la prueba real la ajusta
    ("download_timeout", "DOWNLOAD_TIMEOUT", 180),
    ("download_retries", "F5_DOWNLOAD_RETRIES", 3),
    ("poll_interval", "F5_POLL_INTERVAL", 8.0),
    ("max_poll_failures", "F5_MAX_POLL_FAILURES", 8),
    ("post_timeout", "F5_POST_TIMEOUT", 240),                          # cambio de voz / unify
    ("audit_timeout", "F5_AUDIT_TIMEOUT", 180),                        # ffmpeg + Whisper
    ("claude_timeout", "F5_CLAUDE_TIMEOUT", 90),
    ("claude_qc_retries", "F5_CLAUDE_QC_RETRIES", 2),
    ("ambiguous_submit_retries", "F5_AMBIGUOUS_SUBMIT_RETRIES", 0),    # 0 = un POST ambiguo NUNCA se reenvia solo
    ("safe_conn_retries", "F5_SAFE_CONN_RETRIES", 3),
    ("rate_limit_max_hits", "F5_RATE_LIMIT_MAX_HITS", 8),
    ("post_workers", "F5_POST_WORKERS", 2),
    ("audit_workers", "F5_AUDIT_WORKERS", 1),
    ("claude_qc", "F5_CLAUDE_QC", True),
    ("contract_canary", "F5_CONTRACT_CANARY", True),                   # 1er job de un proveedor sin contrato verificado va SOLO
    ("watchdog_margin", "F5_WATCHDOG_MARGIN", 60),
    ("duration_tolerance", "F5_DURATION_TOLERANCE", 0.25),
]


@dataclass
class F5Config:
    primary_provider: str = "dubvoice"
    fallback_provider: str = "google"
    max_concurrent_video_jobs: int = 3
    video_requests_per_minute: int = 6
    max_attempts_per_clip: int = 3
    max_cost_per_clip: int = 25000
    max_total_generation_time: float = 1200
    submission_timeout: float = 90
    poll_request_timeout: float = 20
    processing_timeout: float = 720
    download_timeout: float = 180
    download_retries: int = 3
    poll_interval: float = 8.0
    max_poll_failures: int = 8
    post_timeout: float = 240
    audit_timeout: float = 180
    claude_timeout: float = 90
    claude_qc_retries: int = 2
    ambiguous_submit_retries: int = 0
    safe_conn_retries: int = 3
    rate_limit_max_hits: int = 8
    post_workers: int = 2
    audit_workers: int = 1
    claude_qc: bool = True
    contract_canary: bool = True
    watchdog_margin: float = 60
    duration_tolerance: float = 0.25

    def limits(self) -> dict:
        """Limites que reciben los proveedores (ver services/dubvoice.py y google_veo.py)."""
        return {"submission": self.submission_timeout, "poll_request": self.poll_request_timeout, "processing": self.processing_timeout,
                "download": self.download_timeout, "download_retries": self.download_retries, "poll_interval": self.poll_interval,
                "max_poll_failures": self.max_poll_failures}

    def watchdog(self, limits: dict | None = None) -> float:
        """Tope duro de UNA llamada completa al proveedor (envio + espera + descarga)."""
        lim = limits or self.limits()
        return lim["submission"] + lim["processing"] + lim["download"] * max(lim["download_retries"], 1) + self.watchdog_margin


_overrides: dict = {}      # solo para tests / uso programatico


def _cast(default, v):
    if isinstance(default, bool):
        return str(v).strip().lower() in ("1", "true", "yes", "si", "on")
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    return str(v)


def config() -> F5Config:
    """Entorno / ~/.zshrc > `data/f5_config.json` > valores por defecto. Se relee en cada corrida (sin cambiar codigo)."""
    filecfg: dict = {}
    f = DATA_DIR / "f5_config.json"
    try:
        if f.exists():
            filecfg = json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        filecfg = {}
    kw = {}
    for attr, env, default in _SPEC:
        v = _overrides.get(attr, _env(env))              # entorno real > ~/.zshrc (y similares) > data/f5_config.json > defecto
        if v in (None, ""):
            v = filecfg.get(env, filecfg.get(attr))
        if v not in (None, ""):
            try:
                kw[attr] = _cast(default, v)
            except (TypeError, ValueError):
                pass
    cfg = F5Config(**kw)
    if str(cfg.fallback_provider).lower() in ("none", "off", "no", "0"):
        cfg.fallback_provider = ""
    return cfg


# ===================================================================== estados
PENDING, SUBMITTED, PROCESSING, VALIDATING = "PENDING", "SUBMITTED", "PROCESSING", "VALIDATING"
ACCEPTED, RETRY_PENDING, FAILED, NEEDS_REVIEW = "ACCEPTED", "RETRY_PENDING", "FAILED", "NEEDS_REVIEW"
STATES = (PENDING, SUBMITTED, PROCESSING, VALIDATING, ACCEPTED, RETRY_PENDING, FAILED, NEEDS_REVIEW)
TERMINAL = {ACCEPTED, FAILED, NEEDS_REVIEW}
IN_FLIGHT = {SUBMITTED, PROCESSING, VALIDATING}

# estados globales de la Fase 5
G_RUNNING, G_COMPLETED, G_WARNINGS, G_FAILED = "RUNNING", "COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED"

TRANSITIONS: dict[str, set[str]] = {
    PENDING: {SUBMITTED, RETRY_PENDING, NEEDS_REVIEW, FAILED},
    SUBMITTED: {PROCESSING, VALIDATING, RETRY_PENDING, NEEDS_REVIEW},
    PROCESSING: {VALIDATING, RETRY_PENDING, NEEDS_REVIEW},
    VALIDATING: {ACCEPTED, RETRY_PENDING, NEEDS_REVIEW},
    RETRY_PENDING: {PENDING, NEEDS_REVIEW, FAILED},
    ACCEPTED: set(),                                   # terminal: jamas se regenera sola (solo por accion explicita: reset_clip)
    FAILED: set(),
    NEEDS_REVIEW: {VALIDATING, PROCESSING, SUBMITTED},  # reanudar desde el RAW o reconciliar un job remoto (gratis)
}


class InvalidTransition(RuntimeError):
    pass


def legacy_status(state: str) -> str:
    """Espejo hacia el campo `status` que leen la UI, F6 y el autopiloto."""
    return {PENDING: "pending", RETRY_PENDING: "pending", SUBMITTED: "running", PROCESSING: "running", VALIDATING: "running",
            ACCEPTED: "done", FAILED: "error", NEEDS_REVIEW: "error"}[state]


# ===================================================================== bitacora de eventos (JSONL, sanitizado)
_ev_lock = threading.Lock()


def now() -> float:
    return time.time()


def event(pid: str, kind: str, **data) -> None:
    """Una linea por transicion/error en data/projects/<id>/f5_events.jsonl. Nunca lanza."""
    try:
        rec = {"ts": round(now(), 3), "t": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **errors.sanitize(data)}
        with _ev_lock, open(store.path(pid, "f5_events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def read_events(pid: str, limit: int = 500) -> list[dict]:
    f = store.pdir(pid) / "f5_events.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ===================================================================== linea de tiempo original (F5 la deriva; F4 no cambia)
def provider_cap(provider: str, model: str | None = None) -> float:
    """Duracion maxima que entrega un proveedor por clip."""
    return 10.0 if (provider == "dubvoice" and model == "omniflash") else 8.0


def compute_timeline(scene: dict, clip: dict, clips: list[dict]) -> dict:
    """source_start / source_end / target_duration = tramo VISUAL que este clip ocupa en el video original.

    El habla (t_start/t_end de F4) es solo una parte del tramo: la escena se reparte entre sus clips cortando por el punto
    medio de las pausas. target_duration NO se infla para español (eso se difiere a audio/F6) ni depende del maximo del proveedor.
    """
    ss, se = scene.get("start"), scene.get("end")
    ts, te = clip.get("t_start"), clip.get("t_end")
    n = len(clips)
    k = next((i for i, c in enumerate(clips) if c is clip), clip.get("idx", 0) if isinstance(clip.get("idx"), int) else 0)
    start = end = None
    src = None
    if isinstance(ss, (int, float)) and isinstance(se, (int, float)) and se > ss:
        if n == 1:
            start, end, src = ss, se, "scene"
        elif all(isinstance(c.get("t_start"), (int, float)) and isinstance(c.get("t_end"), (int, float)) for c in clips):
            start = ss if k == 0 else (clips[k - 1]["t_end"] + clips[k]["t_start"]) / 2
            end = se if k == n - 1 else (clips[k]["t_end"] + clips[k + 1]["t_start"]) / 2
            src = "scene_partition"
            if end <= start:
                start = end = None
        if start is None:
            step = (se - ss) / n
            start, end, src = ss + step * k, ss + step * (k + 1), "scene_equal_split"
    elif isinstance(ts, (int, float)) and isinstance(te, (int, float)) and te > ts:
        start, end, src = ts, te, "clip.t_start/t_end"
    if start is None:
        return {"source_start": None, "source_end": None, "timeline_source": "legacy_target",
                "target_duration": round(min(max(float(clip.get("target") or 4.0), 1.0), 10.0), 2)}
    return {"source_start": round(start, 3), "source_end": round(end, 3), "timeline_source": src,
            "target_duration": round(min(max(end - start, 1.0), 10.0), 2)}


# ===================================================================== estructura clip.f5
def _blank_f5() -> dict:
    return {"state": PENDING, "state_since": now(), "visual_state": "PENDING", "audio_state": "PENDING", "audio_issue": None,
            "source_start": None, "source_end": None, "target_duration": None, "timeline_source": None,
            "round": 1, "attempts": [], "current_attempt": None, "accepted_attempt": None,
            "rate_limit_hits": 0, "safe_conn_retries": 0, "cf_hits": 0, "unknown_hits": 0, "invalid_hits": 0, "ambiguous_used": 0,
            "credits_spent": 0, "gen_seconds": 0.0, "first_submitted_at": None,
            "needs_reconcile": False, "remote_unknown": False, "possible_duplicate": False,
            "review_reason": None, "review_kind": None, "review_message": None, "last_error": None, "history": []}


def ensure(clip: dict, scene: dict, clips: list[dict]) -> dict:
    """Devuelve clip['f5'] (lo crea si no existe). Proyectos anteriores: un clip 'done' con archivo pasa a ACCEPTED sin re-pagar."""
    f5 = clip.get("f5")
    if not isinstance(f5, dict):
        f5 = clip["f5"] = _blank_f5()
        if clip.get("status") == "done" and clip.get("file"):
            f5.update(state=ACCEPTED, visual_state="OK", audio_state="OK" if clip.get("verified") else "UNVERIFIED", legacy=True)
            f5["attempts"].append({"id": "legacy", "n": 0, "round": 1, "provider": clip.get("provider_used"), "model": clip.get("model_used"),
                                   "job_id": clip.get("task_id"), "status": "VALIDATED", "paid": True, "prompt": clip.get("video_prompt"),
                                   "provider_duration": clip.get("duration"), "credits": None, "legacy": True})
            f5["accepted_attempt"] = "legacy"
    if f5.get("target_duration") is None or (f5["state"] == PENDING and not f5["attempts"]):
        f5.update(compute_timeline(scene, clip, clips))          # se congela al primer intento (el historial no cambia de meta)
    for k, v in _blank_f5().items():          # campos nuevos en versiones futuras
        f5.setdefault(k, v)
    return f5


def mirror(clip: dict, f5: dict) -> None:
    """Mantiene actualizados los campos heredados. F6 y la UI siguen leyendo `status`/`file`/`error`."""
    st = f5["state"]
    clip["status"] = legacy_status(st)
    clip["attempts"] = paid_attempts(f5)
    if st in (NEEDS_REVIEW, FAILED):
        clip["error"] = (f"[{f5.get('review_reason') or st}] " + (f5.get("review_message") or ""))[:600]
    elif st != ACCEPTED:
        clip["error"] = None
    if st == ACCEPTED:
        clip["error"] = None
        clip["stale"] = False


@contextmanager
def clip_edit(pid: str, si: int, ci: int):
    """Edita un clip y su bloque f5 de forma atomica (un solo store.edit)."""
    with store.edit(pid) as p:
        scene = p["scenes"][si]
        clips = scene["clips"]
        clip = clips[ci]
        f5 = ensure(clip, scene, clips)
        yield p, clip, f5


def get_f5(pid: str, si: int, ci: int) -> dict:
    p = store.get(pid)
    scene = p["scenes"][si]
    return json.loads(json.dumps(ensure(scene["clips"][ci], scene, scene["clips"])))


def transition(pid: str, si: int, ci: int, new: str, *, force: bool = False, note: str | None = None,
               legacy: dict | None = None, **fields) -> dict:
    """Cambia el estado del clip validando la maquina de estados; persiste y refleja los campos heredados."""
    with clip_edit(pid, si, ci) as (_, clip, f5):
        old = f5["state"]
        if new != old and new not in TRANSITIONS[old] and not force:
            raise InvalidTransition(f"clip {si + 1}.{ci + 1}: {old} → {new} no permitido")
        f5.update(fields)
        f5["state"] = new
        f5["state_since"] = now()
        f5["history"].append({"t": round(now(), 1), "from": old, "to": new, "note": note})
        del f5["history"][:-60]
        mirror(clip, f5)
        if legacy:
            clip.update(legacy)
        snap = {"state": new, "attempt": f5.get("current_attempt"), "reason": f5.get("review_reason")}
    event(pid, "transition", scene=si, clip=ci, frm=old, to=new, note=note, **snap)
    return snap


def new_attempt(pid: str, si: int, ci: int, *, provider: str, model: str | None, prompt: str, asked: float | None,
                credits: int | None, extra: dict | None = None) -> dict:
    """Crea el registro del intento ANTES de enviar el POST (estado SUBMITTING): si la app muere durante el envio queda rastro."""
    with clip_edit(pid, si, ci) as (_, clip, f5):
        n = len(f5["attempts"]) + 1
        att = {"id": f"a{n}", "n": n, "round": f5["round"], "provider": provider, "model": model, "job_id": None,
               "status": "SUBMITTING", "created_at": now(), "submitted_at": None, "last_polled_at": None, "completed_at": None,
               "polls": 0, "error_type": None, "error_message": None, "prompt": prompt, "prompt_rewritten": bool((extra or {}).get("rewritten")),
               "target_duration": f5.get("target_duration"), "asked_seconds": asked, "provider_duration": None,
               "credits": credits, "refund_assumed": False, "paid": False, "submit_ambiguous": False, "possible_duplicate": False,
               "raw": None, "qc": None, **(extra or {})}
        f5["attempts"].append(att)
        f5["current_attempt"] = att["id"]
        if f5.get("first_submitted_at") is None:
            f5["first_submitted_at"] = now()
        snap = dict(att)
    event(pid, "attempt_new", scene=si, clip=ci, attempt=snap["id"], provider=provider, model=model, prompt_sha=_sha(prompt), credits=credits)
    return snap


def patch_attempt(pid: str, si: int, ci: int, att_id: str, **fields) -> None:
    with clip_edit(pid, si, ci) as (_, clip, f5):
        for a in f5["attempts"]:
            if a["id"] == att_id:
                a.update(fields)
                break
        if "credits" in fields or "refund_assumed" in fields or "paid" in fields:
            f5["credits_spent"] = sum((a.get("credits") or 0) for a in f5["attempts"] if a.get("paid") and not a.get("refund_assumed")
                                      and not a.get("legacy") and a.get("round") == f5["round"])
        mirror(clip, f5)


def _sha(text: str | None) -> str:
    import hashlib
    return hashlib.sha1((text or "").encode()).hexdigest()[:10]


def attempt_of(f5: dict, att_id: str | None = None) -> dict | None:
    att_id = att_id or f5.get("current_attempt")
    return next((a for a in f5["attempts"] if a["id"] == att_id), None)


def paid_attempts(f5: dict) -> int:
    """Generaciones pagadas de la ronda actual. El intento 'legacy' (clip migrado de una version anterior) es historial, no un POST de F5."""
    return sum(1 for a in f5["attempts"] if a.get("paid") and not a.get("legacy") and a.get("round") == f5["round"])


def reset_clip(pid: str, si: int, ci: int, *, why: str = "user") -> None:
    """Accion EXPLICITA (regenerar / clip obsoleto): nueva ronda con presupuesto propio. Conserva el historial y los RAW."""
    with clip_edit(pid, si, ci) as (_, clip, f5):
        old = f5["state"]
        keep = {k: f5[k] for k in ("attempts", "history")}
        fresh = _blank_f5()
        fresh.update(keep)
        fresh["round"] = f5["round"] + 1
        for k in ("source_start", "source_end", "target_duration", "timeline_source"):
            fresh[k] = f5.get(k)
        f5.clear()
        f5.update(fresh)
        f5["history"].append({"t": round(now(), 1), "from": old, "to": PENDING, "note": f"reset:{why}"})
        mirror(clip, f5)
    event(pid, "reset", scene=si, clip=ci, frm=old, why=why)


def needs_work(clip: dict) -> bool:
    """¿Este clip debe pasar por F5? ACCEPTED (y no obsoleto) nunca se vuelve a generar."""
    f5 = clip.get("f5")
    if isinstance(f5, dict):
        if f5["state"] == ACCEPTED:
            return bool(clip.get("stale")) or not clip.get("file")
        return True
    return clip.get("status") != "done" or bool(clip.get("stale")) or not clip.get("file")


# ===================================================================== ejecucion acotada (ningun worker queda secuestrado)
def run_bounded(fn, timeout: float, cancel: threading.Event | None = None, name: str = "llamada"):
    """Ejecuta fn() en un hilo daemon. Si excede `timeout` (o se cancela) el llamador SIGUE y el hilo queda abandonado:
    su resultado tardio se descarta. Asi una llamada que no se puede interrumpir (voz, ffmpeg, SDK) no bloquea un worker."""
    box: dict = {}
    done = threading.Event()

    def target():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
        finally:
            done.set()

    threading.Thread(target=target, daemon=True, name=f"f5-{name}").start()
    end = time.time() + timeout
    while not done.wait(0.2):
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelado")
        if time.time() > end:
            raise F5Error(ErrorType.PROVIDER_TIMEOUT, f"{name}: excedio {int(timeout)} s (llamada abandonada)", sub="watchdog")
    if "e" in box:
        raise box["e"]
    return box["v"]


# ===================================================================== autoridad central de concurrencia
class Slots:
    """Slots de GENERACION: cubren unicamente SUBMIT → POLL → DESCARGA DEL RAW. Se liberan en cuanto el RAW esta guardado."""

    def __init__(self):
        self.cv = threading.Condition()
        self.used = 0
        self.by_provider: dict[str, int] = {}
        self.peak = 0
        self.holders: set = set()

    def acquire(self, key, provider: str, cap: int, single, cancel: threading.Event | None, give_up_at: float | None = None) -> None:
        """`single` (bool o callable) = puerta canario: mientras sea verdadera este proveedor solo puede tener UN job en vuelo."""
        with self.cv:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("Cancelado")
                one_only = single() if callable(single) else single
                if self.used < max(cap, 1) and (not one_only or self.by_provider.get(provider, 0) == 0):
                    break
                if give_up_at is not None and time.time() > give_up_at:
                    raise F5Error(ErrorType.PROVIDER_TIMEOUT, "Sin slot de generacion a tiempo", sub="no_slot")
                self.cv.wait(0.5)
            self.used += 1
            self.by_provider[provider] = self.by_provider.get(provider, 0) + 1
            self.holders.add(key)
            self.peak = max(self.peak, self.used)

    def release(self, key, provider: str) -> None:
        with self.cv:
            if key in self.holders:
                self.holders.discard(key)
                self.used = max(self.used - 1, 0)
                self.by_provider[provider] = max(self.by_provider.get(provider, 1) - 1, 0)
                self.cv.notify_all()


class RateGate:
    """Peticiones/minuto por proveedor (envios + sondeos) y cooldown global ante un 429."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stamps: dict[str, list[float]] = {}
        self.cool_until: dict[str, float] = {}

    def cooldown(self, provider: str, seconds: float) -> None:
        with self.lock:
            self.cool_until[provider] = max(self.cool_until.get(provider, 0), time.time() + seconds)

    def acquire(self, provider: str, rpm: int, cancel: threading.Event | None) -> None:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Cancelado")
            with self.lock:
                t = time.time()
                st = [x for x in self.stamps.get(provider, []) if t - x < 60]
                self.stamps[provider] = st
                wait = max(self.cool_until.get(provider, 0) - t, 0)
                if wait <= 0:
                    if len(st) < max(rpm, 1):
                        st.append(t)
                        return
                    wait = st[0] + 60 - t
            time.sleep(min(max(wait, 0.05), 1.0))


class Scheduler:
    """Unica autoridad de F5: slots, ritmo de peticiones, ejecutores de post-proceso y registro de corridas."""

    def __init__(self):
        self.slots = Slots()
        self.rate = RateGate()
        self.lock = threading.RLock()
        self.active: set[tuple] = set()          # (pid, si, ci) con un driver en marcha en ESTE proceso
        self.runs: dict[str, list] = {}
        self._post = None
        self._audit_sem = None
        self._claude_sem = threading.Semaphore(1)

    def post_pool(self):
        from concurrent.futures import ThreadPoolExecutor
        with self.lock:
            if self._post is None:
                self._post = ThreadPoolExecutor(max_workers=max(config().post_workers, 1), thread_name_prefix="f5-post")
            return self._post

    def audit_sem(self):
        with self.lock:
            if self._audit_sem is None:
                self._audit_sem = threading.Semaphore(max(config().audit_workers, 1))
            return self._audit_sem

    def claim(self, key) -> bool:
        with self.lock:
            if key in self.active:
                return False
            self.active.add(key)
            return True

    def unclaim(self, key) -> None:
        with self.lock:
            self.active.discard(key)

    def gate(self, provider: str, cancel):
        cfg = config()
        rpm = cfg.video_requests_per_minute if provider == "dubvoice" else max(cfg.video_requests_per_minute * 5, 30)
        return lambda: self.rate.acquire(provider, rpm, cancel)

    def reset_for_tests(self) -> None:
        with self.lock:
            self.slots = Slots()
            self.rate = RateGate()
            self.active.clear()
            self.runs.clear()
            self._post = None
            self._audit_sem = None


SCHED = Scheduler()


def contract_verified(provider: str) -> bool:
    """¿El contrato HTTP de este proveedor ya se verifico con un ciclo real completo? (solo DubVoice lo necesita)"""
    if provider != "dubvoice":
        return True
    try:
        from ..services import dubvoice
        return dubvoice.contract_verified()
    except Exception:  # noqa: BLE001
        return False


# ===================================================================== politica determinista de reintentos
BACKOFF_RATE = (15, 30, 60, 120)
BACKOFF_CONN = (5, 10, 20)
WAIT_TIMEOUT_RETRY = 5.0        # pausa antes de reintentar tras un timeout / job fallido del proveedor
WAIT_AMBIGUOUS = 60.0           # pausa antes de reenviar un POST ambiguo (solo si F5_AMBIGUOUS_SUBMIT_RETRIES > 0)
WAIT_UNKNOWN = 10.0             # pausa antes del unico reintento de un error desconocido / respuesta invalida
WAIT_DOWNLOAD = 5.0             # pausa antes de reintentar solo la descarga


@dataclass
class Decision:
    kind: str                          # retry | resume | review | failed
    provider: str = "same"             # same | fallback
    rewrite: bool = False
    fix_params: bool = False
    bump_duration: bool = False
    wait: float = 0.0
    consumes: bool = False             # ¿el siguiente intento cuenta contra MAX_ATTEMPTS_PER_CLIP?
    reason: str = ""
    review_kind: str = ""              # REMOTE | POST | LIMIT | PROMPT | QUALITY | PROVIDER
    message: str = ""
    updates: dict = None               # contadores/banderas a persistir en clip.f5
    cooldown: float = 0.0
    disable_provider: str | None = None
    freeze_provider: str | None = None

    def __post_init__(self):
        if self.updates is None:
            self.updates = {}


@dataclass
class Ctx:
    cfg: F5Config
    provider: str                      # proveedor del intento que fallo
    fallback_ok: bool                  # ¿hay un fallback utilizable ahora?
    fallback_name: str = ""
    est_credits: int = 0               # costo estimado del siguiente intento
    reconcile: bool = False            # el error salio de reconciliar un job existente (no de un envio nuevo)
    contract_ok: bool = True


def decide(f5: dict, err: F5Error, ctx: Ctx) -> Decision:
    """Politica exacta por tipo de error. Funcion pura: no toca disco ni red. Claude NO participa."""
    cfg, t = ctx.cfg, err.etype
    n_paid = paid_attempts(f5)
    att = attempt_of(f5)
    job_id = err.job_id or (att or {}).get("job_id")
    used = f5.get("gen_seconds", 0.0)

    def review(reason, kind, msg=None, **u):
        return Decision("review", reason=reason, review_kind=kind, message=(msg or str(err))[:500], updates=u)

    def guard(d: Decision) -> Decision:
        """Topes que aplican a todo lo que compra otra generacion."""
        if d.kind != "retry" or not d.consumes:
            if d.kind == "retry" and used + d.wait > cfg.max_total_generation_time:
                return review("MAX_TOTAL_TIME", "LIMIT", f"Se agotaron {int(cfg.max_total_generation_time // 60)} min de generacion para este clip.")
            return d
        if n_paid >= cfg.max_attempts_per_clip:
            return review("MAX_ATTEMPTS", "LIMIT", f"Se usaron {n_paid} intentos pagados. Ultimo error: {t.value}: {err}")
        if f5.get("credits_spent", 0) + ctx.est_credits > cfg.max_cost_per_clip:
            return review("MAX_COST", "LIMIT", f"Otro intento superaria el tope de {cfg.max_cost_per_clip} creditos por clip.")
        if used + d.wait + 60 > cfg.max_total_generation_time:
            return review("MAX_TOTAL_TIME", "LIMIT", f"Se agotaron {int(cfg.max_total_generation_time // 60)} min de generacion para este clip.")
        return d

    if t == ErrorType.RATE_LIMIT:
        hits = f5.get("rate_limit_hits", 0) + 1
        if hits > cfg.rate_limit_max_hits:
            return review("RATE_LIMIT_EXHAUSTED", "LIMIT", f"{hits - 1} limites de peticiones seguidos.")
        wait = max(err.retry_after or 0, BACKOFF_RATE[min(hits - 1, len(BACKOFF_RATE) - 1)])
        if job_id:                                   # ya existe un job pagado: se sigue sondeando ESE job, jamas se compra otro
            return Decision("resume", wait=wait, cooldown=wait, reason="RATE_LIMIT(job existente)", updates={"rate_limit_hits": hits})
        return guard(Decision("retry", wait=wait, cooldown=wait, reason="RATE_LIMIT", updates={"rate_limit_hits": hits}))

    if t == ErrorType.CONNECTION_ERROR:
        if job_id:
            return review("UNKNOWN_REMOTE_STATE", "REMOTE", f"Se perdio la conexion con el job {job_id}. {err}", remote_unknown=True, needs_reconcile=True)
        if err.ambiguous:
            base = {"possible_duplicate": True, "remote_unknown": True}
            if f5.get("ambiguous_used", 0) < cfg.ambiguous_submit_retries:
                return guard(Decision("retry", wait=WAIT_AMBIGUOUS, consumes=True, reason="AMBIGUOUS_SUBMIT_RETRY",
                                      updates={**base, "ambiguous_used": f5.get("ambiguous_used", 0) + 1}))
            return review("AMBIGUOUS_SUBMIT", "REMOTE",
                          "El envio salio pero se perdio la respuesta: no sabemos si el proveedor creo el job. NO se reenvia automaticamente "
                          "para evitar un doble cobro; revisa el panel del proveedor y pulsa Regenerar solo si decides pagar de nuevo.", **base)
        n = f5.get("safe_conn_retries", 0) + 1
        if n > cfg.safe_conn_retries:
            return review("CONNECTION_ERROR", "LIMIT", f"{n - 1} fallos de conexion seguidos antes de enviar. {err}")
        return guard(Decision("retry", wait=BACKOFF_CONN[min(n - 1, len(BACKOFF_CONN) - 1)], reason="CONNECTION_ERROR", updates={"safe_conn_retries": n}))

    if t == ErrorType.PROVIDER_TIMEOUT:
        if ctx.reconcile and not ctx.contract_ok:
            return review("UNKNOWN_REMOTE_STATE", "REMOTE", f"El job {job_id} no respondio y el contrato de {ctx.provider} aun no esta verificado.",
                          remote_unknown=True, needs_reconcile=True)
        room = cfg.max_total_generation_time - used >= cfg.processing_timeout          # ¿cabe otra ventana completa en el presupuesto del clip?
        can_fb = ctx.fallback_ok and ctx.provider != ctx.fallback_name
        if n_paid < 2 and room:
            return guard(Decision("retry", wait=WAIT_TIMEOUT_RETRY, consumes=True, reason="PROVIDER_TIMEOUT"))
        if can_fb:
            return guard(Decision("retry", provider="fallback", wait=WAIT_TIMEOUT_RETRY, consumes=True, reason="PROVIDER_TIMEOUT→fallback"))
        if n_paid < 2 and cfg.max_total_generation_time - used >= 120:
            return guard(Decision("retry", wait=WAIT_TIMEOUT_RETRY, consumes=True, reason="PROVIDER_TIMEOUT"))     # sin fallback: ventana corta
        return review("PROVIDER_TIMEOUT", "LIMIT", f"{n_paid} intentos sin respuesta a tiempo. {err}")

    if t == ErrorType.PROVIDER_REJECTED:
        if err.sub == "provider_failed":                      # el proveedor reporto el job como fallido (se asume reembolsado)
            if n_paid < 2:
                return guard(Decision("retry", wait=WAIT_TIMEOUT_RETRY, consumes=True, reason="PROVIDER_FAILED"))
            if ctx.fallback_ok and ctx.provider != ctx.fallback_name:
                return guard(Decision("retry", provider="fallback", wait=WAIT_TIMEOUT_RETRY, consumes=True, reason="PROVIDER_FAILED→fallback"))
            return review("PROVIDER_FAILED", "PROVIDER")
        if err.fatal or err.sub in ("auth", "credits", "quota"):
            if ctx.fallback_ok and ctx.provider != ctx.fallback_name:
                return guard(Decision("retry", provider="fallback", reason=f"{err.sub or 'rechazo'}→fallback", disable_provider=ctx.provider))
            return Decision("failed", reason="PROVIDER_UNAVAILABLE", review_kind="PROVIDER", message=str(err)[:500], disable_provider=ctx.provider)
        if not f5.get("params_fixed"):
            return guard(Decision("retry", fix_params=True, reason="PROVIDER_REJECTED→params", updates={"params_fixed": True}))
        if ctx.fallback_ok and ctx.provider != ctx.fallback_name:
            return guard(Decision("retry", provider="fallback", consumes=True, reason="PROVIDER_REJECTED→fallback"))
        return review("PROVIDER_REJECTED", "PROVIDER")

    if t == ErrorType.CONTENT_FILTER:
        cf = f5.get("cf_hits", 0) + 1
        if cf == 1:
            return guard(Decision("retry", rewrite=True, consumes=True, reason="CONTENT_FILTER→rewrite", updates={"cf_hits": cf}))
        if cf == 2 and ctx.fallback_ok and ctx.provider != ctx.fallback_name:
            return guard(Decision("retry", provider="fallback", rewrite=True, consumes=True, reason="CONTENT_FILTER→fallback", updates={"cf_hits": cf}))
        return review("CONTENT_FILTER", "PROMPT", f"El filtro de contenido rechazo {cf} veces (prompt reescrito). {err}", cf_hits=cf)

    if t == ErrorType.INVALID_RESPONSE:
        if err.ambiguous:
            return Decision("review", reason="AMBIGUOUS_SUBMIT", review_kind="REMOTE", freeze_provider=ctx.provider,
                            message=("El proveedor respondio 2xx pero sin un job_id reconocible: puede haber creado (y cobrado) el job. "
                                     f"NO se reenvia. {err}")[:500], updates={"possible_duplicate": True, "remote_unknown": True})
        if job_id:
            return Decision("review", reason="UNKNOWN_REMOTE_STATE", review_kind="REMOTE", freeze_provider=ctx.provider,
                            message=f"Respuesta invalida del job {job_id}. {err}"[:500], updates={"remote_unknown": True, "needs_reconcile": True})
        iv = f5.get("invalid_hits", 0) + 1
        if iv == 1:
            return guard(Decision("retry", consumes=True, wait=WAIT_UNKNOWN, reason="INVALID_RESPONSE", updates={"invalid_hits": iv}))
        if iv == 2 and ctx.fallback_ok and ctx.provider != ctx.fallback_name:
            return guard(Decision("retry", provider="fallback", consumes=True, reason="INVALID_RESPONSE→fallback", updates={"invalid_hits": iv}))
        return review("INVALID_RESPONSE", "PROVIDER", invalid_hits=iv)

    if t == ErrorType.DOWNLOAD_ERROR:
        if not job_id:
            return review("DOWNLOAD_ERROR", "PROVIDER")
        dl = f5.get("download_hits", 0) + 1
        if dl > 2:
            return review("DOWNLOAD_FAILED", "REMOTE", f"El job {job_id} termino pero no se pudo descargar el video ({err}). El job se conserva: "
                          "se reintentara la descarga sin volver a generar.", needs_reconcile=True, download_hits=dl)
        return Decision("resume", wait=WAIT_DOWNLOAD, reason="DOWNLOAD_ERROR", updates={"download_hits": dl})

    if t == ErrorType.QUALITY_REJECTED:
        if err.sub == "duration_short":
            if f5.get("duration_bumped"):
                return review("DURATION_SHORT", "QUALITY", str(err), )
            return guard(Decision("retry", bump_duration=True, consumes=True, reason="DURATION_SHORT", updates={"duration_bumped": True}))
        if n_paid < 2:
            return guard(Decision("retry", rewrite=True, consumes=True, reason="QUALITY_REJECTED"))
        if ctx.fallback_ok and ctx.provider != ctx.fallback_name:
            return guard(Decision("retry", provider="fallback", rewrite=True, consumes=True, reason="QUALITY_REJECTED→fallback"))
        return review("QUALITY_REJECTED", "QUALITY", str(err))

    # UNKNOWN_ERROR
    if job_id:
        return review("UNKNOWN_REMOTE_STATE", "REMOTE", f"Error desconocido con el job {job_id}: {err}", remote_unknown=True, needs_reconcile=True)
    uh = f5.get("unknown_hits", 0) + 1
    if uh == 1:
        return guard(Decision("retry", consumes=True, wait=WAIT_UNKNOWN, reason="UNKNOWN_ERROR", updates={"unknown_hits": uh}))
    return review("UNKNOWN_ERROR", "PROVIDER", f"{err}", unknown_hits=uh)


# ===================================================================== corrida de proyecto y drivers por clip
class PostError(RuntimeError):
    """Falla de infraestructura de post-proceso (Claude, ffmpeg, ...). NUNCA implica regenerar el video: el RAW se conserva."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class _Stop(RuntimeError):
    def __init__(self, reason: str, kind: str, message: str, failed: bool = False):
        super().__init__(message)
        self.reason, self.kind, self.failed = reason, kind, failed


class _Token:
    def __init__(self):
        self.active = True


@dataclass
class Plan:
    provider: str
    model: str | None
    prompt: str
    asked: float | None
    credits: int
    rewritten: bool = False
    reason: str = ""


def est_credits(provider: str, model: str | None, asked: float | None) -> int:
    from ..services import dubvoice
    if provider == "google":
        return 0                    # Google se factura en USD (no en creditos de DubVoice): ver attempt.usd_est
    return int(dubvoice.credits_for(model or "veo-3.1-fast", asked or 8) or 7500)


class Run:
    """Contexto de UNA corrida de F5 sobre un proyecto (supervisor, 'generar clips' o regenerar un clip)."""

    def __init__(self, pid: str, prog=None, log=None, cancel: threading.Event | None = None, explicit: bool = False):
        self.pid, self.explicit = pid, explicit
        self.cfg = config()
        self.cancel = cancel or threading.Event()
        self._prog, self._log = prog, log
        self.disabled: dict[str, str] = {}
        self.frozen: dict[str, str] = {}
        self.invalid: dict[str, int] = {}
        self.notes: dict[tuple, str] = {}
        self.t0 = now()
        self.lock = threading.Lock()

    def log(self, msg: str) -> None:
        event(self.pid, "log", msg=msg)
        try:
            (self._log or self._prog or (lambda *_: None))(msg)
        except Exception:  # noqa: BLE001
            pass

    def progress(self, msg: str, frac: float | None = None) -> None:
        try:
            if self._prog:
                self._prog(msg[:160], frac) if frac is not None else self._prog(msg[:160])
        except Exception:  # noqa: BLE001
            pass

    def usable(self, name: str | None) -> bool:
        if not name or name in self.disabled or name in self.frozen:
            return False
        if name == "google":
            from ..config import get_key
            return bool(get_key("google"))
        return True

    def fallback_name(self) -> str:
        fb = (self.cfg.fallback_provider or "").strip()
        try:
            if store.get(self.pid)["settings"].get("video_fallback", True) is False:      # ajuste heredado del proyecto: sin fallback
                return ""
        except Exception:  # noqa: BLE001
            pass
        return fb if fb and fb != self.cfg.primary_provider else ""

    def fallback_ok(self) -> bool:
        return bool(self.fallback_name()) and self.usable(self.fallback_name())

    def disable(self, provider: str, why: str) -> None:
        with self.lock:
            if provider not in self.disabled:
                self.disabled[provider] = why
                self.log(f"⚠️ {provider} desactivado en esta corrida ({why[:140]}). Sin escalera de modelos: los clips afectados pasan a fallback o a revision.")

    def invalid_seen(self, provider: str, err: F5Error) -> None:
        """Respuesta invalida de un proveedor: si su contrato no esta verificado (o se repite) se DETIENEN los nuevos envios."""
        with self.lock:
            self.invalid[provider] = self.invalid.get(provider, 0) + 1
            n = self.invalid[provider]
        if not contract_verified(provider) or n >= 2:
            self.freeze(provider, f"{err.etype.value}: {str(err)[:120]}")

    def freeze(self, provider: str, why: str) -> None:
        with self.lock:
            if provider not in self.frozen:
                self.frozen[provider] = why
                self.log(f"🧊 {provider}: contrato sospechoso — DETENGO nuevos envios a este proveedor ({why[:160]}). Revisa {store.pdir(self.pid).name}/f5_contract.jsonl")


def _hooks():
    from . import supervisor
    return supervisor


class ClipDriver:
    def __init__(self, run: Run, si: int, ci: int):
        self.run, self.si, self.ci = run, si, ci
        self.pid, self.cfg, self.cancel = run.pid, run.cfg, run.cancel
        self.key = (run.pid, si, ci)
        self.next: Decision | None = None
        self.prompt_fix: str | None = None
        self.label = f"escena {si + 1} clip {ci + 1}"

    # ---------------------------------------------------------------- utilidades
    def f5(self) -> dict:
        return get_f5(self.pid, self.si, self.ci)

    def clip(self) -> dict:
        return store.get(self.pid)["scenes"][self.si]["clips"][self.ci]

    def sleep(self, seconds: float) -> None:
        end = now() + seconds
        while now() < end:
            if self.cancel.is_set():
                raise Cancelled("Cancelado")
            time.sleep(min(0.25, max(end - now(), 0.01)))

    def add_seconds(self, secs: float) -> None:
        with clip_edit(self.pid, self.si, self.ci) as (_, _, f5):
            f5["gen_seconds"] = round(f5.get("gen_seconds", 0.0) + max(secs, 0.0), 2)

    # ---------------------------------------------------------------- flujo principal
    def run_all(self) -> None:
        try:
            self._main()
        except Cancelled:
            self._on_cancel()
        except _Stop as s:
            self._review(s.reason, s.kind, str(s), failed=s.failed)
        except Exception as e:  # noqa: BLE001  - un bug interno no debe tumbar al lote ni perder el RAW
            import traceback
            event(self.pid, "internal_error", scene=self.si, clip=self.ci, error=str(e)[:400], tb=traceback.format_exc()[-1500:])
            self._review("INTERNAL_ERROR", "POST", f"Error interno del motor F5: {type(e).__name__}: {e}", force=True)
        finally:
            SCHED.unclaim(self.key)

    def _main(self) -> None:
        st = self.f5()["state"]
        if st == ACCEPTED:
            return
        if st == NEEDS_REVIEW:
            self._free_recovery()
        elif st in (SUBMITTED, PROCESSING) or self.f5().get("needs_reconcile") and st != VALIDATING:
            self._guarded(self._reconcile, reconcile=True)
        elif st == VALIDATING:
            self._guarded(self._post_and_apply)
        self._attempt_loop()

    def _attempt_loop(self) -> None:
        while True:
            f5 = self.f5()
            if self.cancel.is_set():
                raise Cancelled("Cancelado")
            if f5["state"] == VALIDATING:
                self._guarded(self._post_and_apply)
                continue
            if f5["state"] in (SUBMITTED, PROCESSING):       # p. ej. tras DOWNLOAD_ERROR: reintenta la descarga, no la generacion
                d, self.next = self.next, None
                if d is not None and d.wait:
                    self.sleep(d.wait)
                self._guarded(self._reconcile, reconcile=True)
                continue
            if f5["state"] not in (PENDING, RETRY_PENDING):
                return
            plan = self._plan(f5)
            if plan is None:
                return
            if f5["state"] == RETRY_PENDING:
                transition(self.pid, self.si, self.ci, PENDING, note=(self.next.reason if self.next else "retry"))
            self._guarded(lambda: self._attempt(plan))

    def _guarded(self, fn, reconcile: bool = False) -> None:
        try:
            fn()
        except (Cancelled, _Stop):
            raise
        except PostError as e:
            self._review(e.reason, "POST", str(e))
        except F5Error as e:
            self._apply(e, reconcile=reconcile)
        except Exception as e:  # noqa: BLE001
            f5 = self.f5()
            att = attempt_of(f5)
            self._apply(errors.as_f5(e, provider=(att or {}).get("provider"), job_id=(att or {}).get("job_id")), reconcile=reconcile)

    # ---------------------------------------------------------------- plan del siguiente intento
    def _plan(self, f5: dict) -> Plan | None:
        run, cfg = self.run, self.cfg
        clip = self.clip()
        st = store.get(self.pid)["settings"]
        d, self.next = self.next, None
        if d is None and f5["state"] == RETRY_PENDING and isinstance(f5.get("next"), dict):     # decision persistida (reinicio de la app)
            d = Decision("retry", **{k: v for k, v in f5["next"].items() if k in ("provider", "rewrite", "fix_params", "bump_duration", "reason")})
        last = attempt_of(f5)
        if last is None or d is None:
            provider = cfg.primary_provider
            last = last if d is not None else None
        else:
            provider = last["provider"] if d.provider == "same" else run.fallback_name()
        if provider in run.frozen:
            raise _Stop("PROVIDER_FROZEN", "PROVIDER", f"{provider} congelado: {run.frozen[provider]}")
        if not run.usable(provider):
            fb = run.fallback_name()
            if fb and provider != fb and run.usable(fb):
                provider = fb
            else:
                raise _Stop("PROVIDER_UNAVAILABLE", "PROVIDER", f"{provider} no disponible ({run.disabled.get(provider, 'sin clave')}) y no hay fallback", failed=True)
        if not (store.get(self.pid)["scenes"][self.si].get("image") or {}).get("approved"):
            raise _Stop("NO_START_FRAME", "PROVIDER", f"La imagen de la escena {self.si + 1} no esta aprobada.", failed=True)
        from . import videos
        model = None
        if last and last["provider"] == provider and not (d and d.fix_params):
            model = last.get("model")
        model = model or videos.default_model(st, provider)
        asked = clip.get("target")
        if d and d.bump_duration and last:
            asked = min(8.0, max(float(last.get("asked_seconds") or 4) + 2, float(asked or 0)))
        prompt = last["prompt"] if last else clip["video_prompt"]
        with clip_edit(self.pid, self.si, self.ci) as (_, _, f5w):
            f5w["next"] = None
        rewritten = False
        if d and d.rewrite:
            prompt = self._rewritten(prompt, f5, d.reason)
            rewritten = True
        elif last is None:
            prompt = clip["video_prompt"]
        return Plan(provider, model, prompt, asked, est_credits(provider, model, asked), rewritten, d.reason if d else "first")

    def _rewritten(self, prompt: str, f5: dict, reason: str) -> str:
        used = {" ".join((a.get("prompt") or "").split()) for a in f5["attempts"]}
        fix, self.prompt_fix = self.prompt_fix, None
        cand = fix if fix and " ".join(fix.split()) not in used else None
        if cand is None:
            try:
                cand = run_bounded(lambda: _hooks().rewrite_prompt(self.pid, self.si, self.ci, prompt, reason), self.cfg.claude_timeout,
                                   self.cancel, "reescritura")
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                raise _Stop("PROMPT_REWRITE_UNAVAILABLE", "PROMPT", f"No pude reescribir el prompt ({str(e)[:160]}). No se reenvia el mismo prompt.")
        if not cand or " ".join(cand.split()) in used:
            raise _Stop("PROMPT_REWRITE_UNAVAILABLE", "PROMPT", "La reescritura del prompt no cambio nada: no se reenvia el mismo prompt.")
        return cand

    # ---------------------------------------------------------------- un intento de generacion (SLOT: SUBMIT → POLL → RAW)
    def _attempt(self, plan: Plan) -> None:
        self._acquired_run(plan, resume_job=None)

    def _reconcile(self) -> None:
        f5 = self.f5()
        att = attempt_of(f5)
        if att and att.get("raw") and (store.pdir(self.pid) / att["raw"]).exists():
            if f5["state"] != VALIDATING:
                transition(self.pid, self.si, self.ci, VALIDATING, force=True, note="reconcile:raw en disco", needs_reconcile=False)
            self._post_and_apply()
            return
        if not att or not att.get("job_id"):
            self._review("AMBIGUOUS_SUBMIT" if att and att.get("status") in ("SUBMITTING", "AMBIGUOUS") else "UNKNOWN_REMOTE_STATE", "REMOTE",
                         "No hay job_id registrado para este intento: no se sabe si el proveedor creo un job. NO se reenvia automaticamente.",
                         possible_duplicate=True, remote_unknown=True)
            return
        plan = Plan(att["provider"], att.get("model"), att.get("prompt") or "", att.get("asked_seconds"), 0, False, "reconcile")
        self.run.log(f"🔎 Reconciliando {self.label}: job {att['job_id'][:12]}… en {att['provider']} (sin crear otro)")
        self._acquired_run(plan, resume_job=att)

    def _acquired_run(self, plan: Plan, resume_job: dict | None) -> None:
        from . import videos
        run, cfg = self.run, self.cfg
        provider = plan.provider
        SCHED.slots.acquire(self.key, provider, cfg.max_concurrent_video_jobs, lambda: cfg.contract_canary and not contract_verified(provider),
                            self.cancel)
        t0 = now()
        token = _Token()
        try:
            if provider in run.frozen:
                raise _Stop("PROVIDER_FROZEN", "PROVIDER", f"{provider} congelado: {run.frozen[provider]}")
            if provider in run.disabled:
                raise F5Error(ErrorType.PROVIDER_REJECTED, f"{provider} desactivado en esta corrida: {run.disabled[provider]}", provider=provider,
                              fatal=True, sub="disabled")
            f5 = self.f5()
            limits = cfg.limits()
            remaining = cfg.max_total_generation_time - f5.get("gen_seconds", 0.0)
            if remaining < 60:
                raise _Stop("MAX_TOTAL_TIME", "LIMIT", f"Se agotaron {int(cfg.max_total_generation_time // 60)} min de generacion para este clip.")
            limits["processing"] = max(min(30.0, cfg.processing_timeout), min(cfg.processing_timeout, remaining))
            if resume_job is None:
                att = new_attempt(self.pid, self.si, self.ci, provider=provider, model=plan.model, prompt=plan.prompt,
                                  asked=self._asked_seconds(plan), credits=plan.credits, extra={"rewritten": plan.rewritten, "plan_reason": plan.reason})
                run.log(f"⏳ Enviando {self.label} a {provider} (intento {att['n']})" + (" con prompt reescrito" if plan.rewritten else ""))
            else:
                att = resume_job
                if att.get("submitted_at"):
                    limits["processing"] = max(2 * cfg.poll_interval + 5, min(limits["processing"], cfg.processing_timeout - (now() - att["submitted_at"])))
            att_id = att["id"]
            recorder = self._recorder(provider, att_id)
            beat = {"t": 0.0}

            def on_submit(job_id, meta=None):
                if not token.active:
                    event(self.pid, "late_submit", scene=self.si, clip=self.ci, attempt=att_id, job_id=job_id)
                    return
                self._persist_submit(att_id, job_id, plan)

            def on_poll(n=None, status=None):
                if not token.active:
                    return
                t = now()
                first = beat["t"] == 0.0
                if first or t - beat["t"] > 60:
                    beat["t"] = t
                    self._heartbeat(att_id, n, status, first)

            progress = lambda m: run.notes.__setitem__((self.si, self.ci), m)   # noqa: E731
            if resume_job is None:
                fn = lambda: videos.generate_raw(self.pid, self.si, self.ci, provider=provider, model=plan.model, prompt=plan.prompt,  # noqa: E731
                                                 duration=plan.asked, cancel=self.cancel, progress=progress, on_submit=on_submit,
                                                 on_poll=on_poll, limits=limits, gate=SCHED.gate(provider, self.cancel), recorder=recorder)
            else:
                fn = lambda: videos.resume_raw(provider, resume_job["job_id"], model=plan.model, cancel=self.cancel, progress=progress,  # noqa: E731
                                               on_poll=on_poll, limits=limits, gate=SCHED.gate(provider, self.cancel), recorder=recorder)
            try:
                job_id, data = run_bounded(fn, cfg.watchdog(limits), self.cancel, f"{provider}-{self.si}.{self.ci}")
            except BaseException:
                token.active = False
                raise
            self._raw_saved(att_id, job_id, data)
        except BaseException as e:
            if isinstance(e, F5Error) and e.sub != "disabled" and (e.fatal or e.sub in ("auth", "credits", "quota")):
                run.disable(provider, f"{e.sub or 'rechazo'}: {str(e)[:100]}")          # ANTES de liberar el slot: nadie mas lo intenta
            if isinstance(e, F5Error) and e.etype == ErrorType.INVALID_RESPONSE:
                run.invalid_seen(provider, e)                                            # idem: contrato sospechoso → congelar ya
            self.add_seconds(now() - t0)
            raise
        else:
            self.add_seconds(now() - t0)
        finally:
            SCHED.slots.release(self.key, provider)          # ← el slot se libera aqui: RAW guardado (o intento terminado)
        # ---- fuera del slot: voz / unify / auditoria / Claude
        self._post_and_apply()

    def _asked_seconds(self, plan: Plan):
        from ..services import dubvoice, google_veo
        if plan.provider == "google":
            return google_veo.pick_duration(plan.asked)
        return dubvoice.tier_for(plan.asked or 8, plan.model or "veo-3.1-fast")

    def _recorder(self, provider: str, att_id: str):
        try:
            from ..services import dubvoice
            return dubvoice.ContractRecorder(self.pid, provider, self.si, self.ci, att_id, on_verified=None)
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- persistencia inmediata del job_id
    def _persist_submit(self, att_id: str, job_id: str, plan: Plan) -> None:
        """Se llama en cuanto el proveedor devuelve el job_id (ANTES de esperar el video). Nunca lanza."""
        try:
            with clip_edit(self.pid, self.si, self.ci) as (_, clip, f5):
                for a in f5["attempts"]:
                    if a["id"] == att_id:
                        a.update(job_id=job_id, status="SUBMITTED", submitted_at=now(), paid=True)
                if f5["state"] in (PENDING, RETRY_PENDING):
                    if f5["state"] == RETRY_PENDING:
                        f5["state"] = PENDING
                    f5["history"].append({"t": round(now(), 1), "from": f5["state"], "to": SUBMITTED, "note": f"job {job_id[:16]}"})
                    f5["state"], f5["state_since"] = SUBMITTED, now()
                f5["credits_spent"] = sum((a.get("credits") or 0) for a in f5["attempts"] if a.get("paid") and not a.get("refund_assumed")
                                          and not a.get("legacy") and a.get("round") == f5["round"])
                mirror(clip, f5)
                clip["task_id"] = job_id
            event(self.pid, "job_submitted", scene=self.si, clip=self.ci, attempt=att_id, provider=plan.provider, job_id=job_id)
            self.run.log(f"📨 {self.label}: job {job_id[:12]}… enviado a {plan.provider}")
        except Exception as e:  # noqa: BLE001
            event(self.pid, "job_submitted_UNSAVED", scene=self.si, clip=self.ci, attempt=att_id, job_id=job_id, error=str(e)[:200])

    def _heartbeat(self, att_id: str, n, status, first: bool) -> None:
        try:
            with clip_edit(self.pid, self.si, self.ci) as (_, clip, f5):
                for a in f5["attempts"]:
                    if a["id"] == att_id:
                        a["last_polled_at"] = now()
                        a["polls"] = n if isinstance(n, int) else a.get("polls", 0) + 1
                        if status:
                            a["provider_status"] = str(status)[:40]
                if first and f5["state"] == SUBMITTED:
                    f5["state"], f5["state_since"] = PROCESSING, now()
                    f5["history"].append({"t": round(now(), 1), "from": SUBMITTED, "to": PROCESSING, "note": str(status)[:40]})
                mirror(clip, f5)
        except Exception:  # noqa: BLE001
            pass

    def _raw_saved(self, att_id: str, job_id: str, data: bytes) -> None:
        """Guarda el MP4 pagado (atomico) y pasa a VALIDATING. Desde aqui el RAW ya no se pierde por ningun fallo posterior."""
        f5 = self.f5()
        att = attempt_of(f5, att_id)
        if att.get("job_id") is None:                          # proveedor simulado que no llamo on_submit
            self._persist_submit(att_id, job_id, Plan(att["provider"], att.get("model"), att.get("prompt") or "", None, 0))
        raw = store.path(self.pid, "videos", f"s{self.si:02d}_c{self.ci}_{att_id}_raw.mp4")
        tmp = raw.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(raw)
        rel = store.rel(self.pid, raw)
        info = run_bounded(lambda: media.probe(raw), 30, self.cancel, "ffprobe")
        if not info.get("has_video") or info["duration"] < 0.5:
            patch_attempt(self.pid, self.si, self.ci, att_id, raw=None, bad_raw=rel)
            raise F5Error(ErrorType.DOWNLOAD_ERROR, "El archivo descargado no es un MP4 de video valido", job_id=job_id, sub="corrupt")
        patch_attempt(self.pid, self.si, self.ci, att_id, status="DOWNLOADED", completed_at=now(), raw=rel, job_id=job_id, paid=True,
                      provider_duration=round(info["duration"], 2))
        transition(self.pid, self.si, self.ci, VALIDATING, note="raw guardado", legacy={"raw": rel, "task_id": job_id}, needs_reconcile=False)
        self.run.log(f"💾 {self.label}: RAW guardado ({round(info['duration'], 1)} s) — slot liberado, sigue validacion")

    # ---------------------------------------------------------------- post-proceso (FUERA del slot)
    def _post_and_apply(self) -> None:
        res = self._post()
        self._apply_post(res)

    def _post(self) -> dict:
        cfg = self.cfg
        budget = cfg.claude_timeout * (cfg.claude_qc_retries + 1) + cfg.post_timeout + cfg.audit_timeout + 120
        fut = SCHED.post_pool().submit(self._post_work)
        end = now() + budget
        import concurrent.futures as cf
        while True:
            try:
                return fut.result(timeout=0.25)
            except cf.TimeoutError:
                if self.cancel.is_set():
                    raise Cancelled("Cancelado")
                if now() > end:
                    raise PostError("POST_TIMEOUT", f"El post-proceso de {self.label} excedio {int(budget)} s (RAW conservado).")

    def _post_work(self) -> dict:
        """Corre en el ejecutor de post-proceso. NO cambia el estado del clip: devuelve un veredicto que aplica el driver."""
        from . import videos
        cfg = self.cfg
        f5 = self.f5()
        att = attempt_of(f5)
        raw = store.pdir(self.pid) / att["raw"]
        pdur = att.get("provider_duration")
        if pdur is None:
            pdur = round(media.probe(raw)["duration"], 2)
        target = f5.get("target_duration") or 4.0
        need = min(target, provider_cap(att["provider"], att.get("model")))
        out: dict = {"att": att["id"], "pdur": pdur, "target": target}
        # 1) material visual suficiente para el tramo que se usara
        if pdur + cfg.duration_tolerance < need:
            out["reject"] = F5Error(ErrorType.QUALITY_REJECTED, f"El clip dura {pdur} s y el tramo necesita {need} s.", sub="duration_short",
                                    provider=att["provider"])
            return out
        window = min(pdur, target + 0.5)         # zona que realmente se usara: F6 recorta el resto
        # 2) QC visual (Claude): solo mira imagen
        if cfg.claude_qc:
            qc = self._qc(raw, window)
            out["qc"] = qc
            if not qc.get("visual_ok"):
                out["reject"] = F5Error(ErrorType.QUALITY_REJECTED, "; ".join(qc.get("reasons") or ["rechazo visual"])[:300], sub="visual",
                                        provider=att["provider"])
                out["prompt_fix"] = qc.get("prompt_fix")
                return out
        # 3) voz unificada — el RAW ya esta a salvo; cualquier fallo aqui solo deja el audio original de Veo
        final, warn = raw, None
        try:
            final, warn = run_bounded(lambda: videos.finish_clip(self.pid, self.si, self.ci, raw), cfg.post_timeout, self.cancel, "voz")
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            final, warn = raw, f"No se pudo unificar la voz ({str(e)[:200]}). Se conserva la voz de Veo; el RAW esta guardado y se puede repetir con retry_voice."
        out["final"], out["voice_warning"] = str(final), warn
        # 4) auditoria de audio (ffmpeg + Whisper): informa, NUNCA regenera el video
        audit = None
        try:
            with SCHED.audit_sem():
                audit = run_bounded(lambda: _hooks().audit_clip(self.pid, self.si, self.ci, final, window), cfg.audit_timeout, self.cancel, "auditoria")
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            out["audit_error"] = str(e)[:200]
        out["audit"] = audit
        out["duration"] = round(media.probe(final)["duration"], 2)
        return out

    def _qc(self, raw, window: float) -> dict:
        cfg = self.cfg
        last = "sin respuesta"
        for i in range(cfg.claude_qc_retries + 1):
            if self.cancel.is_set():
                raise Cancelled("Cancelado")
            try:
                with SCHED._claude_sem:
                    qc = run_bounded(lambda: _hooks().qc_visual(self.pid, self.si, self.ci, raw, window), cfg.claude_timeout, self.cancel, "qc-claude")
                if isinstance(qc, dict) and isinstance(qc.get("visual_ok"), bool):
                    return qc
                last = f"respuesta sin 'visual_ok': {str(qc)[:120]}"
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = str(e)[:200]
            if i < cfg.claude_qc_retries:
                self.sleep(3 * (i + 1))
        raise PostError("QC_UNAVAILABLE", f"El QC visual de Claude no esta disponible ({last}). El RAW esta guardado y NO se regenera; "
                                          "se reanuda solo la validacion al volver a lanzar F5.")

    def _apply_post(self, res: dict) -> None:
        att_id = res["att"]
        if res.get("reject") is not None:
            err: F5Error = res["reject"]
            patch_attempt(self.pid, self.si, self.ci, att_id, status="REJECTED", qc=res.get("qc"), error_type=err.etype.value, error_message=str(err)[:300])
            self.prompt_fix = res.get("prompt_fix")
            self.run.log(f"❌ Rechazado {self.label}: {str(err)[:140]}")
            self._apply(err)
            return
        f5 = self.f5()
        att = attempt_of(f5, att_id)
        clip = self.clip()
        final = Path(res["final"])
        audio_state, issue = evaluate_audio(res.get("audit"), clip, res.get("voice_warning"), res.get("audit_error"))
        warn = "; ".join(x for x in (res.get("voice_warning"), (f"Audio por revisar: {issue}" if audio_state == "NEEDS_FIX" and not res.get("voice_warning") else None)) if x) or None
        patch_attempt(self.pid, self.si, self.ci, att_id, status="VALIDATED", qc=res.get("qc"), completed_at=now())
        legacy = {"file": store.rel(self.pid, final), "raw": att["raw"], "duration": res["duration"], "task_id": att.get("job_id"),
                  "provider_used": att["provider"], "model_used": att.get("model"), "asked_seconds": att.get("asked_seconds"),
                  "verified": True if res.get("qc") else None, "warning": warn, "video_prompt": att.get("prompt") or clip.get("video_prompt"),
                  "audit": ({k: v for k, v in res["audit"].items() if k != "frames"} if res.get("audit") else None), "stale": False}
        transition(self.pid, self.si, self.ci, ACCEPTED, note="visual OK", legacy=legacy, visual_state="OK", audio_state=audio_state, audio_issue=issue,
                   accepted_attempt=att_id, needs_reconcile=False, remote_unknown=False, review_reason=None, review_kind=None, review_message=None)
        self.run.log(f"✅ Aceptado {self.label} (visual OK; {att['provider']}, {res['pdur']} s de material para un tramo de {res['target']} s"
                     + (f"; audio: {audio_state}" if audio_state != "OK" else "") + ")")

    # ---------------------------------------------------------------- aplicar la decision de la politica
    def _apply(self, err: F5Error, reconcile: bool = False) -> None:
        run, cfg = self.run, self.cfg
        f5 = self.f5()
        att = attempt_of(f5)
        if err.etype == ErrorType.PROVIDER_TIMEOUT and not err.job_id and att and not att.get("job_id") and att.get("status") == "SUBMITTING":
            err = F5Error(ErrorType.CONNECTION_ERROR, f"El envio no respondio a tiempo: {err}", ambiguous=True, sub="submit_timeout",
                          provider=err.provider or att.get("provider"))
        provider = err.provider or (att or {}).get("provider") or cfg.primary_provider
        job_id = err.job_id or (att or {}).get("job_id")
        if att:                                   # 1) registrar el intento (paid/job_id/estado) ANTES de decidir: los topes lo cuentan
            fields = {"error_type": err.etype.value, "error_message": str(err)[:400], "completed_at": att.get("completed_at") or now()}
            if att.get("status") in ("SUBMITTING", "SUBMITTED", "PROCESSING", "DOWNLOADED"):
                fields["status"] = "ABANDONED" if err.etype == ErrorType.PROVIDER_TIMEOUT else "ERROR"
            if err.ambiguous and not att.get("job_id"):
                fields.update(submit_ambiguous=True, possible_duplicate=True, paid=True, status="AMBIGUOUS")
            if err.etype == ErrorType.CONTENT_FILTER or err.sub == "provider_failed":
                fields["refund_assumed"] = True
            if job_id and not att.get("job_id"):
                fields.update(job_id=job_id, paid=True)
            if err.etype == ErrorType.PROVIDER_TIMEOUT and job_id:
                fields["abandoned_job_may_still_bill"] = True
            patch_attempt(self.pid, self.si, self.ci, att["id"], **fields)
        f5 = self.f5()
        ctx = Ctx(cfg, provider, run.fallback_ok(), run.fallback_name(), est_credits(provider, (att or {}).get("model"), (att or {}).get("asked_seconds")),
                  reconcile, contract_verified(provider))
        d = decide(f5, err, ctx)
        event(self.pid, "error", scene=self.si, clip=self.ci, attempt=(att or {}).get("id"), error=err.as_dict(), decision=d.kind, reason=d.reason)
        if d.cooldown:
            SCHED.rate.cooldown(provider, d.cooldown)
            run.log(f"🐢 {provider}: limite de peticiones — pausa global de {int(d.cooldown)} s")
        upd = dict(d.updates)
        upd["last_error"] = err.as_dict()
        with clip_edit(self.pid, self.si, self.ci) as (_, _, f5w):
            f5w.update(upd)
        if d.disable_provider and err.sub != "disabled":
            run.disable(d.disable_provider, f"{err.sub or err.etype.value}: {str(err)[:100]}")
        if d.kind == "retry":
            self.next = d
            upd_next = {k: getattr(d, k) for k in ("provider", "rewrite", "fix_params", "bump_duration", "reason")}
            transition(self.pid, self.si, self.ci, RETRY_PENDING, note=d.reason, next=upd_next)
            n = paid_attempts(self.f5())
            run.log(f"🔁 Reintento {self.label} ({d.reason}; {err.etype.value}; intentos pagados {n}/{cfg.max_attempts_per_clip})"
                    + (f" — espero {int(d.wait)} s" if d.wait else ""))
            if d.wait:
                self.add_seconds(d.wait)
                self.sleep(d.wait)
                d.wait = 0.0
        elif d.kind == "resume":
            self.next = d
            run.log(f"⬇️ {self.label}: reintento la DESCARGA del job {str(job_id)[:12]}… (sin volver a generar)")
        elif d.kind == "failed":
            self._review(d.reason, d.review_kind, d.message, failed=True, **d.updates)
        else:
            self._review(d.reason, d.review_kind, d.message, **d.updates)

    def _review(self, reason: str, kind: str, message: str, failed: bool = False, force: bool = False, **updates) -> None:
        state = self.f5()["state"]
        if state == ACCEPTED:
            return
        never_paid = not any(a.get("paid") or a.get("job_id") for a in self.f5()["attempts"])
        target = FAILED if (failed and state in (PENDING, RETRY_PENDING) and never_paid) else NEEDS_REVIEW
        transition(self.pid, self.si, self.ci, target, force=force or state == NEEDS_REVIEW, note=reason,
                   review_reason=reason, review_kind=kind, review_message=errors.scrub(message, 600), **updates)
        self.run.log(f"{'⛔' if target == FAILED else '🟠'} {self.label} → {target} [{reason}] {errors.scrub(message, 220)}")

    def _free_recovery(self) -> None:
        """Clip en NEEDS_REVIEW: solo acciones GRATIS (reanudar validacion desde el RAW o reconciliar un job existente)."""
        f5 = self.f5()
        att = attempt_of(f5)
        if f5.get("review_kind") == "POST" and att and att.get("raw") and (store.pdir(self.pid) / att["raw"]).exists():
            self.run.log(f"♻️ {self.label}: reanudo la validacion desde el RAW guardado (sin regenerar)")
            transition(self.pid, self.si, self.ci, VALIDATING, note="resume_from_raw", review_reason=None, review_kind=None, review_message=None)
            self._guarded(self._post_and_apply)
        elif f5.get("review_kind") == "REMOTE" and att and (att.get("job_id") or att.get("raw")) and not f5.get("free_recovery_done"):
            self.run.log(f"♻️ {self.label}: intento recuperar el job existente antes de considerar pagar otro")
            with clip_edit(self.pid, self.si, self.ci) as (_, _, f5w):
                f5w["free_recovery_done"] = True
            transition(self.pid, self.si, self.ci, PROCESSING, note="free_recovery", review_reason=None, review_kind=None, review_message=None)
            self._guarded(self._reconcile, reconcile=True)

    def _on_cancel(self) -> None:
        try:
            with clip_edit(self.pid, self.si, self.ci) as (_, clip, f5):
                if f5["state"] in IN_FLIGHT:
                    f5["needs_reconcile"] = True
                    clip["status"], clip["error"] = "error", "Detenido; el job remoto se conserva y se reconcilia al reanudar (no se vuelve a pagar)."
                elif f5["state"] in (PENDING, RETRY_PENDING):
                    clip["status"] = "pending"
        except Exception:  # noqa: BLE001
            pass


def evaluate_audio(audit: dict | None, clip: dict, voice_warning: str | None, audit_error: str | None) -> tuple[str, str | None]:
    """Estado de AUDIO (independiente del visual). NEEDS_FIX/UNVERIFIED nunca provocan una nueva generacion de video."""
    if voice_warning:
        return "NEEDS_FIX", "cambio de voz fallido o expirado (se conserva la voz original de Veo)"
    if audit is None:
        return "UNVERIFIED", f"auditoria de audio no disponible ({audit_error or 'sin datos'})"
    if not audit.get("has_audio"):
        return "NEEDS_FIX", "el clip no tiene audio"
    if clip.get("dialogue"):
        if audit.get("speech_seconds", 0) < 1.0:
            return "NEEDS_FIX", "casi no hay voz en el clip"
        if audit.get("transcript_error"):
            return "UNVERIFIED", f"no se pudo transcribir ({audit['transcript_error']})"
        if "match" in audit and audit["match"] < 0.5:
            return "NEEDS_FIX", f"el dialogo hablado no coincide con el guion (match {audit['match']})"
    return "OK", None


# ===================================================================== recuperacion tras cerrar/reabrir
def recover_after_restart(p: dict, active: set | None = None) -> int:
    """Se ejecuta con el proyecto abierto para edicion (jobs.reset_stale) o al iniciar una corrida.
    Un intento SUBMITTING sin job_id es AMBIGUO → NEEDS_REVIEW (jamas se reenvia solo). Un job conocido queda para RECONCILIAR."""
    changed = 0
    active = active if active is not None else SCHED.active
    for si, s in enumerate(p.get("scenes", [])):
        for ci, c in enumerate(s.get("clips", [])):
            f5 = c.get("f5")
            if not isinstance(f5, dict) or (p["id"], si, ci) in active or f5["state"] == ACCEPTED:
                continue
            att = attempt_of(f5)
            if att and att.get("status") == "SUBMITTING" and not att.get("job_id"):
                att.update(status="AMBIGUOUS", submit_ambiguous=True, possible_duplicate=True, paid=True, error_type="CONNECTION_ERROR",
                           error_message="La app se cerro durante el envio: no se sabe si el proveedor creo el job.")
                f5.update(state=NEEDS_REVIEW, state_since=now(), possible_duplicate=True, remote_unknown=True, review_reason="AMBIGUOUS_SUBMIT",
                          review_kind="REMOTE", review_message="La app se cerro mientras se enviaba el clip: no se sabe si el proveedor creo (y cobro) un job. "
                          "NO se reenvia automaticamente. Revisa el panel del proveedor; pulsa Regenerar solo si decides pagar de nuevo.")
                f5["history"].append({"t": round(now(), 1), "from": "?", "to": NEEDS_REVIEW, "note": "restart:SUBMITTING sin job_id"})
                mirror(c, f5)
                changed += 1
            elif f5["state"] in IN_FLIGHT:
                f5["needs_reconcile"] = True
                c["status"], c["error"] = "error", "Interrumpido; el job se reconcilia al reanudar (no se vuelve a pagar)."
                changed += 1
    return changed


# ===================================================================== estado global y telemetria
OPEN_STATES = {PENDING, SUBMITTED, PROCESSING, VALIDATING, RETRY_PENDING}


def clip_state(c: dict) -> str:
    f5 = c.get("f5")
    if isinstance(f5, dict):
        return f5["state"]
    return ACCEPTED if (c.get("status") == "done" and c.get("file")) else PENDING


def global_state(p: dict) -> str:
    states = [clip_state(c) for s in p.get("scenes", []) for c in s.get("clips", [])]
    if not states:
        return G_FAILED
    if any(x in OPEN_STATES for x in states):
        return G_RUNNING
    if all(x == ACCEPTED for x in states):
        return G_COMPLETED
    if all(x == FAILED for x in states):
        return G_FAILED
    return G_WARNINGS


def summarize(p: dict) -> dict:
    """Resumen de telemetria de F5 + historial de intentos por clip (consultable)."""
    from collections import Counter
    clips = []
    counts: Counter = Counter()
    err_types: Counter = Counter()
    prov: dict[str, dict] = {}
    gen_times: list[float] = []
    credits = 0
    review, audio_fix, dup = [], [], []
    attempts_total = paid_total = retries = 0
    for si, s in enumerate(p.get("scenes", [])):
        for ci, c in enumerate(s.get("clips", [])):
            f5 = c.get("f5") if isinstance(c.get("f5"), dict) else None
            st = clip_state(c)
            counts[st] += 1
            if not f5:
                continue
            atts = f5.get("attempts", [])
            real = [a for a in atts if not a.get("legacy")]          # el intento migrado (legacy) se conserva en el historial pero NO es un POST de F5
            attempts_total += len(real)
            cur = [a for a in real if a.get("round") == f5.get("round")]
            retries += max(len(cur) - 1, 0)
            for a in real:
                d = prov.setdefault(a.get("provider") or "?", {"attempts": 0, "accepted": 0, "paid": 0})
                d["attempts"] += 1
                if a.get("paid"):
                    d["paid"] += 1
                    paid_total += 1
                    if not a.get("refund_assumed"):
                        credits += a.get("credits") or 0
                if a.get("status") == "VALIDATED":
                    d["accepted"] += 1
                if a.get("error_type"):
                    err_types[a["error_type"]] += 1
                if a.get("submitted_at") and a.get("completed_at") and a.get("status") in ("DOWNLOADED", "VALIDATED", "REJECTED"):
                    gen_times.append(a["completed_at"] - a["submitted_at"])
                if a.get("possible_duplicate"):
                    dup.append({"scene": si + 1, "clip": ci + 1, "attempt": a["id"], "job_id": a.get("job_id")})
            row = {"scene": si + 1, "clip": ci + 1, "state": st, "visual_state": f5.get("visual_state"), "audio_state": f5.get("audio_state"),
                   "target_duration": f5.get("target_duration"), "source_start": f5.get("source_start"), "source_end": f5.get("source_end"),
                   "attempts": [{k: a.get(k) for k in ("id", "provider", "model", "job_id", "status", "submitted_at", "last_polled_at", "completed_at", "polls",
                                                       "error_type", "error_message", "target_duration", "provider_duration", "credits", "possible_duplicate", "legacy")}
                                for a in atts]}
            clips.append(row)
            if st in (NEEDS_REVIEW, FAILED):
                review.append({"scene": si + 1, "clip": ci + 1, "state": st, "reason": f5.get("review_reason"), "kind": f5.get("review_kind"),
                               "message": f5.get("review_message"), "job_ids": [a.get("job_id") for a in atts if a.get("job_id")],
                               "paid_attempts": paid_attempts(f5)})
            if f5.get("audio_state") in ("NEEDS_FIX", "UNVERIFIED") and st == ACCEPTED:
                audio_fix.append({"scene": si + 1, "clip": ci + 1, "audio_state": f5["audio_state"], "issue": f5.get("audio_issue")})
    run = p.get("f5_run") or {}
    wall = None
    if run.get("started_at"):
        wall = round((run.get("finished_at") or now()) - run["started_at"], 1)
    total = sum(counts.values())
    return {"state": global_state(p), "clips_total": total, "by_state": dict(counts), "accepted": counts[ACCEPTED],
            "needs_review": counts[NEEDS_REVIEW], "failed": counts[FAILED], "wall_seconds": wall,
            "attempts_total": attempts_total, "paid_attempts": paid_total, "retries": retries,
            "per_provider": prov, "errors_by_type": dict(err_types),
            "avg_generation_seconds": round(sum(gen_times) / len(gen_times), 1) if gen_times else None,
            "max_generation_seconds": round(max(gen_times), 1) if gen_times else None,
            "credits_estimated": credits, "possible_duplicates": dup, "needs_review_list": review, "audio_needs_fix": audio_fix,
            "contract": {"dubvoice_verified": contract_verified("dubvoice")}, "clips": clips}


# ===================================================================== corrida de proyecto (punto de entrada unico)
def _select(pid: str, keys, explicit: bool, run: Run) -> list[tuple[int, int]]:
    p = store.get(pid)
    all_keys = [(si, ci) for si, s in enumerate(p["scenes"]) for ci, _ in enumerate(s.get("clips", []))]
    chosen = list(keys) if keys else all_keys
    out = []
    for si, ci in chosen:
        c = p["scenes"][si]["clips"][ci]
        if not isinstance(c.get("f5"), dict) and (needs_work(c) or (explicit and keys)):
            with clip_edit(pid, si, ci):                       # proyecto antiguo: crea su bloque f5 (un clip 'done' pasa a ACCEPTED sin pagar)
                pass
            p = store.get(pid)
            c = p["scenes"][si]["clips"][ci]
        f5 = c.get("f5") if isinstance(c.get("f5"), dict) else None
        st = f5["state"] if f5 else None
        if not needs_work(c) and not (explicit and keys):
            continue
        if st == ACCEPTED and (explicit or c.get("stale") or not c.get("file")):
            reset_clip(pid, si, ci, why="explicit" if explicit else "stale")
        elif st == FAILED:
            reset_clip(pid, si, ci, why="retry_failed")
        elif st == NEEDS_REVIEW:
            kind = f5.get("review_kind")
            att = attempt_of(f5)
            has_raw = bool(att and att.get("raw") and (store.pdir(pid) / att["raw"]).exists())
            has_job = bool(att and att.get("job_id"))
            free = (kind == "POST" and has_raw) or (kind == "REMOTE" and (has_job or has_raw) and not f5.get("free_recovery_done"))
            if free:
                pass                                        # el driver intenta primero la recuperacion GRATIS
            elif explicit:
                reset_clip(pid, si, ci, why="user_regenerate")      # decision explicita del usuario: nueva ronda pagada
            else:
                continue                                    # una corrida automatica nunca vuelve a pagar un clip en revision
        elif st == PENDING and f5 is None:
            pass
        out.append((si, ci))
    return out


def run_project(pid: str, keys=None, *, prog=None, log=None, cancel: threading.Event | None = None, explicit: bool = False) -> dict:
    """Ejecuta F5 sobre los clips pendientes de un proyecto. Devuelve el resumen. Solo falla si F5 NO puede ejecutarse."""
    run = Run(pid, prog, log, cancel, explicit)
    with SCHED.lock:
        SCHED.runs.setdefault(pid, []).append(run)
    try:
        from ..config import KEY_ALIASES, get_key
        for svc in KEY_ALIASES:
            errors.register_secret(get_key(svc))
        with store.edit(pid) as p:
            recover_after_restart(p)
        todo = _select(pid, keys, explicit, run)
        with store.edit(pid) as p:
            p["f5_run"] = {"state": G_RUNNING, "started_at": now(), "finished_at": None, "clips": len(todo), "config": {
                k: getattr(run.cfg, k) for k in ("primary_provider", "fallback_provider", "max_concurrent_video_jobs", "video_requests_per_minute",
                                                 "max_attempts_per_clip", "max_total_generation_time", "processing_timeout")}}
        if not todo:
            run.log("Todos los clips ya estaban listos (o esperan una accion tuya: ver clips en revision).")
        else:
            run.log(f"🚀 F5 iniciada: {len(todo)} clip(s) · {run.cfg.max_concurrent_video_jobs} slots de generacion · "
                    f"primario {run.cfg.primary_provider}" + (f" · fallback {run.fallback_name()}" if run.fallback_name() else " · sin fallback"))
        drivers = []
        for si, ci in todo:
            if SCHED.claim((pid, si, ci)):
                drivers.append(ClipDriver(run, si, ci))
        if drivers:
            from concurrent.futures import ThreadPoolExecutor, wait
            with ThreadPoolExecutor(max_workers=min(len(drivers), 24), thread_name_prefix="f5-clip") as ex:
                futs = [ex.submit(d.run_all) for d in drivers]
                while True:
                    done, pending = wait(futs, timeout=1.0)
                    p = store.get(pid)
                    counts = {}
                    for c in (c for s in p["scenes"] for c in s["clips"]):
                        counts[clip_state(c)] = counts.get(clip_state(c), 0) + 1
                    total = max(sum(counts.values()), 1)
                    fin = counts.get(ACCEPTED, 0) + counts.get(NEEDS_REVIEW, 0) + counts.get(FAILED, 0)
                    run.progress(f"{counts.get(ACCEPTED, 0)}/{total} aceptados · {counts.get(SUBMITTED, 0) + counts.get(PROCESSING, 0)} generando · "
                                 f"{counts.get(VALIDATING, 0)} validando · {counts.get(NEEDS_REVIEW, 0)} en revision", fin / total)
                    if not pending:
                        break
                for f in futs:
                    f.result()
        return finalize(pid, run, cancelled=run.cancel.is_set())
    except Exception as e:  # noqa: BLE001
        with store.edit(pid) as p:
            p["f5_run"] = {**(p.get("f5_run") or {}), "state": G_FAILED, "finished_at": now(), "error": errors.scrub(str(e), 500)}
        event(pid, "run_failed", error=str(e)[:400])
        raise
    finally:
        with SCHED.lock:
            if run in SCHED.runs.get(pid, []):
                SCHED.runs[pid].remove(run)


def finalize(pid: str, run: Run, cancelled: bool = False) -> dict:
    with store.edit(pid) as p:
        summ = summarize({**p, "f5_run": {**(p.get("f5_run") or {}), "finished_at": now()}})
        state = summ["state"]
        p["f5_run"] = {**(p.get("f5_run") or {}), "state": state, "finished_at": now(), "cancelled": cancelled,
                       "summary": {k: v for k, v in summ.items() if k != "clips"}}
    event(pid, "run_finished", state=state, accepted=summ["accepted"], needs_review=summ["needs_review"], failed=summ["failed"])
    ok, tot = summ["accepted"], summ["clips_total"]
    if cancelled:
        run.log(f"⏹ Detenido: {ok}/{tot} clips aceptados; los jobs en curso se conservan y se reconcilian al reanudar.")
    else:
        rv = "; ".join(f"escena {x['scene']} clip {x['clip']} [{x['reason']}]" for x in summ["needs_review_list"])
        run.log(f"🏁 Terminado: {ok}/{tot} clips aceptados · estado F5: {state}" + (f" · requieren revision: {rv}" if rv else "")
                + f" (≈{summ['credits_estimated']:,} creditos)")
        if summ["audio_needs_fix"]:
            run.log(f"🎙️ {len(summ['audio_needs_fix'])} clip(s) aceptados visualmente con audio por revisar (retry_voice): no se regeneraron.")
    return {**summ, "state": state}


def stop_project(pid: str) -> None:
    with SCHED.lock:
        for r in SCHED.runs.get(pid, []):
            r.cancel.set()

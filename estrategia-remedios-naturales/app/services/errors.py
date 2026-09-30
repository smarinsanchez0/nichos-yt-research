"""Errores tipados de la Fase 5 + sanitizacion de secretos.

Toda falla de un proveedor de video se convierte en un `F5Error` con un `ErrorType`; la politica de reintentos
(`phases/video_jobs.py`) decide SOLO por ese tipo, nunca leyendo texto libre.
"""
from __future__ import annotations

import os
import re
from enum import Enum


class ErrorType(str, Enum):
    RATE_LIMIT = "RATE_LIMIT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    CONTENT_FILTER = "CONTENT_FILTER"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class Cancelled(RuntimeError):
    """El usuario (o el motor) cancelo la operacion. No es un error del proveedor."""


class F5Error(RuntimeError):
    """Error tipado.

    ambiguous  : la peticion de creacion (POST) pudo haber llegado al proveedor pero no conocemos el job_id
                 -> NUNCA se reenvia automaticamente (riesgo de doble cobro).
    sub        : detalle dentro del tipo (auth, credits, quota, duration_short, no_job_id, ...).
    fatal      : el proveedor no sirve para el resto de la corrida (auth/creditos/cuota).
    """

    def __init__(self, etype: ErrorType, message: str, *, retry_after: float | None = None, provider: str | None = None,
                 job_id: str | None = None, ambiguous: bool = False, http_status: int | None = None,
                 sub: str | None = None, fatal: bool = False, result_url: str | None = None):
        super().__init__(sanitize(message))
        self.etype = ErrorType(etype)
        self.retry_after = retry_after
        self.provider = provider
        self.job_id = job_id
        self.ambiguous = ambiguous
        self.http_status = http_status
        self.sub = sub
        self.fatal = fatal
        self.result_url = result_url

    def as_dict(self) -> dict:
        return {"type": self.etype.value, "sub": self.sub, "message": str(self)[:600], "http_status": self.http_status,
                "ambiguous": self.ambiguous, "job_id": self.job_id, "retry_after": self.retry_after}


# ------------------------------------------------------------------ clasificacion
_CONTENT = ("content policy", "content_policy", "safety", "prohibited", "blocked", "moderation", "raimediafiltered", "filtro de seguridad",
            "politica de contenido", "sensitive", "violates", "responsible ai", "usage guidelines")
_TIMEOUT = ("no termino en", "tardo demasiado", "timed out", "timeout", "time out", "no respondio a tiempo")
_QUOTA = ("resource_exhausted", "quota", "billing", "exceeded your current")
_CREDITS = ("creditos insuficientes", "insufficient credit", "not enough credit", "payment required")
_AUTH = ("falta la api key", "unauthorized", "invalid api key", "api key not valid", "permission denied", "api_key_invalid", "forbidden")
_CONN = ("sin conexion", "connection reset", "connection refused", "connection aborted", "remote end closed", "network is unreachable",
         "temporary failure in name resolution", "server disconnected", "eof occurred")
_INVALID = ("no devolvio id", "no devolvio operacion", "respuesta no json", "sin url de resultado", "respuesta inesperada", "no devolvio video")
_DOWNLOAD = ("descarga", "download")


def classify(exc: BaseException | str, http_status: int | None = None) -> ErrorType:
    """Mapea cualquier excepcion/mensaje (incluidos los RuntimeError heredados) a un ErrorType."""
    if isinstance(exc, F5Error):
        return exc.etype
    text = str(exc).lower()
    m = re.search(r"respondio (\d{3})", text)
    status = http_status or (int(m.group(1)) if m else None)
    if any(k in text for k in _CONTENT):
        return ErrorType.CONTENT_FILTER
    if any(k in text for k in _DOWNLOAD) and not any(k in text for k in _QUOTA):
        return ErrorType.DOWNLOAD_ERROR
    if status in (401, 402, 403) or any(k in text for k in _QUOTA) or any(k in text for k in _CREDITS) or any(k in text for k in _AUTH):
        return ErrorType.PROVIDER_REJECTED
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return ErrorType.RATE_LIMIT
    if any(k in text for k in _TIMEOUT):
        return ErrorType.PROVIDER_TIMEOUT
    if any(k in text for k in _INVALID):
        return ErrorType.INVALID_RESPONSE
    if status and status >= 500 or any(k in text for k in _CONN):
        return ErrorType.CONNECTION_ERROR
    if status and 400 <= status < 500:
        return ErrorType.PROVIDER_REJECTED
    return ErrorType.UNKNOWN_ERROR


def sub_of(exc: BaseException | str, http_status: int | None = None) -> str | None:
    text = str(exc).lower()
    m = re.search(r"respondio (\d{3})", text)
    status = http_status or (int(m.group(1)) if m else None)
    if any(k in text for k in _QUOTA):
        return "quota"
    if status == 402 or any(k in text for k in _CREDITS):
        return "credits"
    if status in (401, 403) or any(k in text for k in _AUTH):
        return "auth"
    return None


def as_f5(exc: BaseException, *, provider: str | None = None, job_id: str | None = None) -> F5Error:
    """Devuelve `exc` si ya es F5Error; si no, lo tipa por clasificacion (conservando job_id)."""
    if isinstance(exc, F5Error):
        if provider and not exc.provider:
            exc.provider = provider
        if job_id and not exc.job_id:
            exc.job_id = job_id
        return exc
    et = classify(exc)
    sub = sub_of(exc)
    return F5Error(et, str(exc), provider=provider, job_id=job_id, sub=sub, fatal=sub in ("auth", "credits", "quota"))


# ------------------------------------------------------------------ sanitizacion
_SECRET_KEY = re.compile(r"(api[-_]?key|token|secret|authorization|signature|password|passwd|credential|x-goog-|bearer|cookie)", re.I)
_SECRET_TEXT = [
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9_\-]{4,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)(x-api-key|api[-_]?key|authorization)\s*[:=]\s*\S+"),
]
_URL_QUERY = re.compile(r"(https?://[^\s\"'?]+)\?[^\s\"']*")
_DATA_URI = re.compile(r"data:[a-z/+\-]+;base64,[A-Za-z0-9+/=]{40,}")
_ENV_NAMES = ("DUBVOICE_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_AI_STUDIO_API_KEY", "ANTHROPIC_API_KEY",
              "ELEVENLABS_API_KEY", "KIE_API_KEY", "PEXELS_API_KEY")
_extra_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Registra un valor secreto conocido para que se redacte tal cual aparezca en cualquier texto."""
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def _known_secrets() -> set[str]:
    out = set(_extra_secrets)
    for n in _ENV_NAMES:
        v = os.environ.get(n)
        if v and len(v) >= 6:
            out.add(v)
    return out


def scrub(text: str, limit: int = 4000) -> str:
    """Redacta claves/tokens/firmas en un texto libre."""
    if not isinstance(text, str):
        text = str(text)
    for s in sorted(_known_secrets(), key=len, reverse=True):
        text = text.replace(s, "<redacted>")
    for rx in _SECRET_TEXT:
        text = rx.sub("<redacted>", text)
    text = _DATA_URI.sub(lambda m: f"<data-uri {len(m.group(0))} chars>", text)
    text = _URL_QUERY.sub(r"\1?<query-redacted>", text)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


def sanitize(obj, limit: int = 4000):
    """Copia segura para registrar: redacta valores de claves sensibles, tokens en texto, queries de URL y base64."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if isinstance(k, str) and _SECRET_KEY.search(k) else sanitize(v, limit)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(x, limit) for x in obj[:200]]
    if isinstance(obj, bytes):
        return f"<bytes {len(obj)}>"
    if isinstance(obj, str):
        return scrub(obj, limit)
    return obj


def shape(obj, depth: int = 0):
    """Arbol de claves/tipos de un JSON (para descubrir el contrato sin registrar valores)."""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:60]}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1), f"len={len(obj)}"] if obj else []
    if isinstance(obj, str):
        return f"str({len(obj)})"
    return type(obj).__name__

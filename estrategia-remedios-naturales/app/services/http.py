from __future__ import annotations

import time

import httpx

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def request(method: str, url: str, *, retries: int = 4, timeout: float = 300, **kw) -> httpx.Response:
    last: Exception | None = None
    for i in range(retries):
        try:
            r = httpx.request(method, url, timeout=timeout, **kw)
            if r.status_code in RETRY_STATUS and i < retries - 1:
                time.sleep(2 ** (i + 1))
                continue
            return r
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            time.sleep(2 ** (i + 1))
    raise RuntimeError(f"Sin conexion con {url.split('?')[0]}: {last}")


def fail(service: str, r: httpx.Response) -> RuntimeError:
    body = r.text[:500].replace("\n", " ")
    hint = ""
    if r.status_code in (401, 403):
        hint = " (revisa que la API key sea valida y tenga permisos/credito)"
    return RuntimeError(f"{service} respondio {r.status_code}{hint}: {body}")


# ===================================================================== Fase 5: peticiones acotadas (sin reintentos internos)
def _retry_after(r) -> float | None:
    try:
        v = r.headers.get("Retry-After")
        return float(v) if v else None
    except (ValueError, AttributeError, TypeError):
        return None


def request_once(method: str, url: str, *, connect: float = 10, read: float = 30, deadline: float = 60, cancel=None,
                 max_bytes: int = 20_000_000, **kw) -> httpx.Response:
    """UNA peticion con timeouts por fase y deadline total; nunca reintenta (F5 decide). Comprueba `cancel` entre trozos.

    Los fallos se tipan como F5Error(CONNECTION_ERROR). `ambiguous=True` si la peticion ya se habia enviado completa
    (la respuesta se perdio): para un POST de creacion eso significa "no sabemos si el proveedor creo el job".
    """
    from .errors import Cancelled, ErrorType, F5Error
    t0 = time.time()
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelado")
    remaining = max(deadline, 1.0)
    timeout = httpx.Timeout(connect=min(connect, remaining), read=min(read, remaining), write=min(max(read, 30), remaining), pool=connect)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=bool(kw.pop("follow_redirects", False))) as client:
            with client.stream(method, url, **kw) as resp:
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise F5Error(ErrorType.INVALID_RESPONSE, f"Respuesta demasiado grande de {url.split('?')[0]}", sub="too_large")
                    if time.time() - t0 > deadline:
                        raise F5Error(ErrorType.CONNECTION_ERROR, f"Deadline de {int(deadline)} s agotado leyendo {url.split('?')[0]}",
                                      ambiguous=True, sub="deadline")
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("Cancelado")
                return httpx.Response(resp.status_code, headers=resp.headers, content=b"".join(chunks), request=resp.request)
    except (F5Error, Cancelled):
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteError, httpx.WriteTimeout) as e:
        raise F5Error(ErrorType.CONNECTION_ERROR, f"Sin conexion con {url.split('?')[0]}: {type(e).__name__}", ambiguous=False, sub="not_sent")
    except httpx.HTTPError as e:            # ReadError, ReadTimeout, RemoteProtocolError, ...: la peticion ya salio
        raise F5Error(ErrorType.CONNECTION_ERROR, f"Sin conexion con {url.split('?')[0]}: {type(e).__name__}: {str(e)[:120]}",
                      ambiguous=True, sub="after_send")


def typed_error(service: str, r, provider: str | None = None) -> "F5Error":
    """Convierte una respuesta HTTP no exitosa en F5Error tipado."""
    from .errors import ErrorType, F5Error, classify, sub_of
    body = (getattr(r, "text", "") or "")[:500].replace("\n", " ")
    st = r.status_code
    msg = f"{service} respondio {st}: {body}"
    sub = sub_of(msg, st)
    et = classify(msg, st)
    if st == 429 and sub != "quota":
        et = ErrorType.RATE_LIMIT
    if et == ErrorType.UNKNOWN_ERROR and 400 <= st < 500:
        et = ErrorType.PROVIDER_REJECTED
        sub = sub or "params"
    if st in (408, 425):
        et = ErrorType.CONNECTION_ERROR
    return F5Error(et, msg, provider=provider, http_status=st, retry_after=_retry_after(r), sub=sub,
                   fatal=sub in ("auth", "credits", "quota"))


def download(url: str, *, headers: dict | None = None, deadline: float = 180, connect: float = 10, read: float = 30, cancel=None,
             max_bytes: int = 300_000_000) -> bytes:
    """Descarga en streaming con deadline total, timeout por trozo y cancelacion. Falla como F5Error(DOWNLOAD_ERROR)."""
    from .errors import Cancelled, ErrorType, F5Error
    t0 = time.time()
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=connect, read=read, write=read, pool=connect), follow_redirects=True) as c:
            with c.stream("GET", url, headers=headers or {}) as resp:
                if resp.status_code != 200:
                    raise F5Error(ErrorType.DOWNLOAD_ERROR, f"Descarga respondio {resp.status_code}", http_status=resp.status_code)
                want = int(resp.headers.get("content-length") or 0)
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise F5Error(ErrorType.DOWNLOAD_ERROR, "Descarga demasiado grande", sub="too_large")
                    if time.time() - t0 > deadline:
                        raise F5Error(ErrorType.DOWNLOAD_ERROR, f"Descarga excedio {int(deadline)} s", sub="deadline")
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("Cancelado")
                if want and size < want:
                    raise F5Error(ErrorType.DOWNLOAD_ERROR, f"Descarga incompleta ({size}/{want} bytes)", sub="truncated")
                if size == 0:
                    raise F5Error(ErrorType.DOWNLOAD_ERROR, "Descarga vacia", sub="empty")
                return b"".join(chunks)
    except (F5Error, Cancelled):
        raise
    except httpx.HTTPError as e:
        raise F5Error(ErrorType.DOWNLOAD_ERROR, f"Descarga fallo: {type(e).__name__}: {str(e)[:120]}")

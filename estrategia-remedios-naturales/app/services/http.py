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

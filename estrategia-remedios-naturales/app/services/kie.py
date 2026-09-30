"""Kie.ai: subida de archivos y Nano Banana (opcional). El video ya NO se genera aqui: se usa DubVoice."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from ..config import require_key
from .http import fail, request

API = "https://api.kie.ai"
UPLOAD = "https://kieai.redpandaai.co/api/file-base64-upload"


def _h() -> dict:
    return {"Authorization": f"Bearer {require_key('kie')}", "Content-Type": "application/json"}


def _check(service: str, r):
    if r.status_code != 200:
        raise fail(service, r)
    j = r.json()
    if j.get("code") not in (200, None):
        raise RuntimeError(f"{service}: {j.get('msg') or j}")
    return j


def upload_image(path: Path) -> str:
    """Sube la imagen a Kie y devuelve una URL publica temporal (las APIs de video la exigen)."""
    from PIL import Image
    import io
    im = Image.open(path).convert("RGB")
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode()
    body = {"base64Data": f"data:image/jpeg;base64,{b64}", "uploadPath": "ern/images",
            "fileName": f"{path.stem}-{int(time.time())}.jpg"}
    r = request("POST", UPLOAD, json=body, headers=_h(), timeout=180)
    j = _check("Kie (subida)", r)
    d = j.get("data") or {}
    url = d.get("downloadUrl") or d.get("fileUrl") or d.get("url")
    if not url:
        raise RuntimeError(f"Kie (subida) no devolvio URL: {j}")
    return url


def _find_urls(obj) -> list[str]:
    urls: list[str] = []
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _find_urls(json.loads(s))
            except json.JSONDecodeError:
                return []
        if s.startswith("http"):
            urls.append(s)
    elif isinstance(obj, list):
        for x in obj:
            urls += _find_urls(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("resulturls", "resulturl", "originurls", "videourl", "imageurl", "url", "fullresulturls"):
                urls += _find_urls(v)
            elif isinstance(v, (dict, list)) or (isinstance(v, str) and v[:1] in "{["):
                urls += _find_urls(v)
    return urls


def download(url: str) -> bytes:
    r = request("GET", url, timeout=600, follow_redirects=True)
    if r.status_code != 200:
        raise fail("Descarga de resultado", r)
    return r.content


# --------------------------------------------------------------- Nano Banana en Kie
def nano_banana_edit(prompt: str, image_paths: list[Path], model: str = "google/nano-banana-edit",
                     aspect: str = "9:16", progress=None, timeout: float = 600) -> bytes:
    urls = [upload_image(p) for p in image_paths]
    body = {"model": model, "input": {"prompt": prompt, "image_urls": urls,
                                       "output_format": "png", "image_size": aspect}}
    r = request("POST", f"{API}/api/v1/jobs/createTask", json=body, headers=_h())
    tid = ((_check("Kie (imagen)", r).get("data") or {}).get("taskId"))
    if not tid:
        raise RuntimeError("Kie no devolvio taskId para la imagen")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(4)
        g = request("GET", f"{API}/api/v1/jobs/recordInfo", params={"taskId": tid}, headers=_h())
        d = (_check("Kie (imagen)", g).get("data") or {})
        state = (d.get("state") or "").lower()
        if state == "success":
            found = _find_urls(d.get("resultJson"))
            if found:
                return download(found[0])
            raise RuntimeError("Kie termino pero sin URL de imagen")
        if state in ("fail", "failed"):
            raise RuntimeError(f"Kie fallo al generar la imagen: {d.get('failMsg') or d.get('failCode')}")
    raise RuntimeError("Kie tardo demasiado generando la imagen")


# --------------------------------------------------------------- Veo (imagen -> video con audio)
def upload_file(path: Path, mime: str = "audio/mpeg", folder: str = "ern/audio") -> str:
    """Sube un archivo (audio) a Kie y devuelve una URL publica temporal."""
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    body = {"base64Data": f"data:{mime};base64,{b64}", "uploadPath": folder,
            "fileName": f"{Path(path).stem}-{int(time.time())}{Path(path).suffix}"}
    r = request("POST", UPLOAD, json=body, headers=_h(), timeout=300)
    d = _check("Kie (subida)", r).get("data") or {}
    url = d.get("downloadUrl") or d.get("fileUrl") or d.get("url")
    if not url:
        raise RuntimeError("Kie (subida) no devolvio URL")
    return url

"""Cliente minimo de la API de Anthropic (Messages) con vision y salida JSON."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from ..config import env, require_key
from .http import fail, request

URL = "https://api.anthropic.com/v1/messages"


def image_block(path: Path, max_side: int | None = None) -> dict:
    data = Path(path).read_bytes()
    if max_side:
        data = _shrink(data, max_side)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(data).decode()}}


def _shrink(data: bytes, max_side: int) -> bytes:
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def ask(content: list[dict] | str, *, system: str = "", model: str, max_tokens: int = 8000,
        temperature: float | None = None) -> str:
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    headers = {"x-api-key": require_key("anthropic"), "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    ws = env("ANTHROPIC_WORKSPACE_ID")
    if ws:  # necesario si la key no esta ligada a un workspace
        headers["anthropic-workspace-id"] = ws
    r = request("POST", URL, json=body, timeout=600, headers=headers)
    if r.status_code == 400 and "workspace" in r.text and not ws:
        raise RuntimeError(
            "Tu key de Anthropic no esta ligada a un workspace. Solucion: agrega en ~/.zshrc la linea "
            "`export ANTHROPIC_WORKSPACE_ID=wrkspc_...` (Console > Settings > Workspaces) o crea una key dentro de un "
            "workspace, y reabre la app.")
    if r.status_code != 200:
        raise fail("Anthropic", r)
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for o, c in (("{", "}"), ("[", "]")):
        a, b = t.find(o), t.rfind(c)
        if a != -1 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Claude no devolvio JSON valido: " + text[:300])


def ask_json(content, *, system: str = "", model: str, max_tokens: int = 8000):
    system = (system + "\n\n" if system else "") + "Responde UNICAMENTE con JSON valido, sin texto adicional ni markdown."
    text = ask(content, system=system, model=model, max_tokens=max_tokens)
    try:
        return parse_json(text)
    except ValueError:
        # un reintento pidiendo correccion
        fix = ask([{"type": "text", "text": "Tu respuesta anterior no era JSON valido. Devuelve el mismo contenido "
                    "como JSON valido y nada mas:\n\n" + text[:12000]}], system=system, model=model, max_tokens=max_tokens)
        return parse_json(fix)

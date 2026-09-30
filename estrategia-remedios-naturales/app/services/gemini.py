"""Google AI Studio: Nano Banana (Gemini image) para generar/editar imagenes con referencias."""
from __future__ import annotations

import base64
import io
from pathlib import Path

from ..config import require_key
from .http import fail, request

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _part_image(path: Path) -> dict:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((1280, 1280))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


def generate_image(prompt: str, refs: list[tuple[str, Path]], model: str, aspect: str = "9:16") -> bytes:
    """refs: [(etiqueta, ruta)] en orden; cada etiqueta precede a su imagen."""
    parts: list[dict] = []
    for label, p in refs:
        parts.append({"text": label})
        parts.append(_part_image(p))
    parts.append({"text": prompt})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                 "imageConfig": {"aspectRatio": aspect}}}
    r = request("POST", f"{BASE}/{model}:generateContent", json=body, timeout=300,
                headers={"x-goog-api-key": require_key("google"), "content-type": "application/json"})
    if r.status_code != 200:
        raise fail("Google AI Studio", r)
    j = r.json()
    cands = j.get("candidates") or []
    for c in cands:
        for part in (c.get("content") or {}).get("parts", []):
            d = part.get("inlineData") or part.get("inline_data")
            if d and d.get("data"):
                return base64.b64decode(d["data"])
    reason = (cands[0].get("finishReason") if cands else None) or (j.get("promptFeedback") or {}).get("blockReason")
    txt = " ".join(p.get("text", "") for c in cands for p in (c.get("content") or {}).get("parts", []))[:300]
    raise RuntimeError(f"Nano Banana no devolvio imagen (motivo: {reason or 'desconocido'}). {txt}".strip())

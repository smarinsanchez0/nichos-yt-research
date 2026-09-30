"""FASE 1: perfil del avatar (descripcion para prompts + perfil de voz recomendado)."""
from __future__ import annotations

from .. import store
from ..services import claude
from .common import abs_path

SYSTEM = """Eres director de casting y de arte para videos de venta para Instagram Reels dirigidos a
publico de EE.UU. Analizas la foto de un avatar y devuelves datos precisos para (a) escribir prompts
de imagen que mantengan al personaje consistente y (b) elegir la voz mas adecuada."""

PROMPT = """Analiza a la persona de la foto. Devuelve JSON con:
{
 "description": "descripcion en INGLES, muy concreta y visual, para prompts de imagen (edad aparente, etnia/tono de piel, cabello, vello facial, ojos, rasgos distintivos, ropa y accesorios visibles). 60-90 palabras",
 "gender": "male" | "female",
 "age_range": "ej. 35-45",
 "voice": {"gender": "male|female", "age": "young|middle_aged|old", "tone": "ej. warm, trustworthy, energetic",
           "energy": "low|medium|high", "accent": "american"},
 "summary_es": "resumen breve en español del personaje y de la voz recomendada"
}"""


def run(pid: str, prog) -> None:
    p = store.get(pid)
    if not p["avatar"]:
        raise RuntimeError("Primero sube la foto del avatar.")
    prog("Analizando el avatar con Claude…", 0.3)
    img = claude.image_block(abs_path(pid, p["avatar"]["file"]), max_side=1280)
    data = claude.ask_json([img, {"type": "text", "text": PROMPT}], system=SYSTEM,
                           model=p["settings"]["claude_model"], max_tokens=1500)
    with store.edit(pid) as q:
        q["avatar"]["profile"] = data

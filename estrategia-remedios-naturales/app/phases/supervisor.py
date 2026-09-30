"""SUPERVISOR de la Fase 5 (autoridad REDUCIDA).

Antes Claude decidia todo (reintentos, timeouts, cambio de modelo/proveedor, cuando rendirse). Ahora un motor determinista
(`video_jobs`: maquina de estados + politica por tipo de error) decide timeouts, rate limits, errores de conexion, limites de
intentos, fallback y estado global. Claude solo hace dos cosas:

  1. `qc_visual`: mira el avatar de referencia + fotogramas del clip y dice si el VIDEO es visualmente valido.
  2. `rewrite_prompt`: reescribe un prompt solo ante un problema semantico/visual (filtro de contenido o rechazo visual).

Una llamada a Claude por clip terminado, no un ciclo continuo. El audio/dialogo NO provoca regenerar el video.
La API publica (`start`, `stop`) se conserva.
"""
from __future__ import annotations

import json
import threading
import time

from .. import media, store
from ..services import claude, stt
from . import editing, video_jobs
from .common import abs_path

_active: dict[str, "Supervisor"] = {}

QC_SYSTEM = """Eres el control de calidad VISUAL de un equipo que replica Reels (9:16) con un avatar de IA.
Recibes la imagen aprobada de referencia (el avatar y el ambiente esperados) y 3 fotogramas del clip generado (inicio, medio, final del tramo que se usara).
Responde ACEPTANDO (visual_ok=true) si: (1) la persona es el avatar de la referencia (misma cara, ropa y ambiente, sin deformaciones graves);
(2) hace la accion pedida; (3) no hay subtitulos, marcas de agua ni texto superpuesto generado.
NO evalues audio, voz ni duracion: eso lo mide otro sistema. No rechaces por diferencias menores de iluminacion o pose.
Responde UNICAMENTE con JSON: {"visual_ok": true|false, "reasons": ["motivo corto", ...], "prompt_fix": "(solo si rechazas por un problema semantico/visual que un prompt distinto pueda corregir) prompt de video completo reescrito, o vacio"}"""

REWRITE_SYSTEM = """Reescribes prompts de video (Veo 3.1) para que pasen el filtro de contenido o corrijan un problema visual, SIN cambiar el dialogo hablado
ni la accion principal. Suaviza expresiones sensibles (salud, promesas, cuerpo), simplifica y refuerza el encuadre. Devuelve UNICAMENTE JSON: {"prompt": "prompt completo reescrito"}"""


# ---------------------------------------------------------------- auditoria tecnica (audio) — informa, no decide
def audit_clip(pid: str, si: int, ci: int, path, window: float) -> dict:
    p = store.get(pid)
    st = p["settings"]
    c = p["scenes"][si]["clips"][ci]
    info = media.probe(path)
    a = {"duration": round(info["duration"], 1), "has_audio": info["has_audio"], "window": round(window, 1)}
    if info["has_audio"]:
        segs = media.speech_segments(path, info["duration"])
        a["speech_seconds"] = round(sum(b - x for x, b in segs), 1)
        a["ends_with_speech"] = bool(segs) and segs[-1][1] >= info["duration"] - 0.15
        if c.get("dialogue"):
            try:
                wav = media.extract_audio(path, store.path(pid, "work", "audit", f"s{si}_c{ci}.mp3"))
                tr = stt.transcribe(wav, st, language_code=st.get("output_language", "es"))
                a["transcript"] = tr["text"][:300]
                a["match"] = round(editing.overlap(c["dialogue"], tr["text"]), 2)
            except Exception as e:  # noqa: BLE001  - Whisper caido: el RAW y el video siguen validos
                a["transcript_error"] = str(e)[:120]
    return a


def _frames(pid: str, si: int, ci: int, raw, window: float) -> list:
    dur = max(min(media.probe(raw)["duration"], window), 0.6)
    out = []
    for i, frac in enumerate((0.1, 0.5, 0.9)):
        out.append(media.extract_frame(raw, dur * frac, store.path(pid, "work", "audit", f"s{si}_c{ci}_f{i}.jpg"), width=384))
    return out


def qc_visual(pid: str, si: int, ci: int, raw, window: float) -> dict:
    """Una llamada a Claude: ¿el video es visualmente valido? (nunca mira ni decide sobre el audio)."""
    p = store.get(pid)
    c = p["scenes"][si]["clips"][ci]
    content: list[dict] = [
        {"type": "text", "text": f"Accion pedida: {c.get('action_en') or c.get('action_es') or '(sin accion especifica)'}\nEncuadre: {c.get('camera') or '-'}"},
        {"type": "text", "text": "Imagen aprobada de referencia (el avatar y el ambiente esperados):"},
        claude.image_block(abs_path(pid, p["scenes"][si]["image"]["file"]), 384),
        {"type": "text", "text": "Fotogramas del clip generado (inicio, medio, final del tramo usado):"},
    ]
    for fr in _frames(pid, si, ci, raw, window):
        content.append(claude.image_block(fr, 384))
    content.append({"type": "text", "text": "QC VISUAL: decide ahora."})
    data = claude.ask_json(content, system=QC_SYSTEM, model=p["settings"]["claude_model"], max_tokens=700)
    return {"visual_ok": data.get("visual_ok"), "reasons": [str(x)[:160] for x in (data.get("reasons") or [])][:5],
            "prompt_fix": (data.get("prompt_fix") or "").strip() or None}


def rewrite_prompt(pid: str, si: int, ci: int, prompt: str, reason: str) -> str:
    p = store.get(pid)
    c = p["scenes"][si]["clips"][ci]
    txt = (f"REESCRIBE PROMPT (motivo: {reason}). Dialogo que NO debe cambiar: \"{c.get('dialogue') or ''}\"\n\nPROMPT ACTUAL:\n{prompt}")
    data = claude.ask_json([{"type": "text", "text": txt}], system=REWRITE_SYSTEM, model=p["settings"]["claude_model"], max_tokens=1200)
    return (data.get("prompt") or "").strip()


# ---------------------------------------------------------------- envoltorio con la API heredada
class Supervisor:
    def __init__(self, pid: str, prog):
        self.pid, self.prog = pid, prog
        self.stop_flag = threading.Event()

    def log(self, msg: str) -> None:
        with store.edit(self.pid) as p:
            j = p["jobs"].setdefault("supervisor", {})
            lg = j.setdefault("log", [])
            lg.append({"t": time.strftime("%H:%M:%S"), "msg": msg[:600]})
            del lg[:-120]
        try:
            self.prog(msg[:160])
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> dict:
        p = store.get(self.pid)
        if p["settings"].get("unify_voice") and not p["settings"].get("voice_id"):
            raise RuntimeError("Elige una voz (o desactiva 'unificar voz') antes de usar el supervisor.")
        summary = video_jobs.run_project(self.pid, prog=self.prog, log=self.log, cancel=self.stop_flag)
        if summary["state"] == video_jobs.G_FAILED:
            raise RuntimeError("Fase 5 no pudo ejecutarse: " + "; ".join(f"escena {x['scene']} clip {x['clip']}: {x['message']}"
                                                                        for x in summary["needs_review_list"])[:700])
        return summary


def start(pid: str, prog) -> dict:
    sup = Supervisor(pid, prog)
    _active[pid] = sup
    try:
        return sup.run()
    finally:
        _active.pop(pid, None)


def stop(pid: str) -> None:
    sup = _active.get(pid)
    if sup:
        sup.stop_flag.set()
    video_jobs.stop_project(pid)

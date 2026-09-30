"""Servicios SIMULADOS para probar todo el flujo sin API keys ni creditos (`--demo`). Usa ffmpeg real."""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
from pathlib import Path

from PIL import Image

from . import media

TMP = Path(tempfile.mkdtemp(prefix="ern-demo-media-"))
SCRIPT = ("Did you know that ginger tea can calm your stomach in minutes. "
          "Grandma used this simple remedy every single day. "
          "Today I will show you three natural recipes that actually work. "
          "Get the free book at the link below and start now")


def make_source(path: Path) -> Path:
    """Video de prueba: 15 s, 3 escenas de color, tono con pausas (simula voz)."""
    media.run(["-f", "lavfi", "-i", "color=c=red:s=360x640:d=5:r=30", "-f", "lavfi", "-i", "color=c=green:s=360x640:d=5:r=30",
               "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=5:r=30",
               "-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*220*t)*lt(mod(t,4),2.5)':d=15:s=44100",
               "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    return path


def make_avatar(path: Path) -> Path:
    Image.new("RGB", (576, 1024), (120, 90, 60)).save(path, "JPEG")
    return path


def jpg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (576, 1024), (120, 90, 60)).save(b, "JPEG")
    return b.getvalue()


def mp3(sec: float = 8) -> bytes:
    f = TMP / "v.mp3"
    media.run(["-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*500*t)*lt(t,4.2)':d={sec}:s=44100", str(f)])
    return f.read_bytes()


def fake_video(prompt: str) -> bytes:
    out = TMP / f"veo_{abs(hash(prompt))}.mp4"
    media.run(["-f", "lavfi", "-i", "testsrc=s=360x640:d=8:r=24", "-f", "lavfi", "-i",
               "aevalsrc='0.5*sin(2*PI*300*t)*lt(t,4.2)':d=8:s=44100", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", str(out)])
    return out.read_bytes()


def fake_ask_json(content, *, system="", model="", max_tokens=0):
    if isinstance(content, str):
        if content.startswith("Traduce cada linea"):
            rows = [l.split("\t", 1) for l in content.split("\n\n", 1)[1].splitlines()]
            return {"translations": [{"i": int(i), "es": "ES: " + t} for i, t in rows]}
        if content.startswith("Traduce al español natural"):
            items = json.loads(content.split("\n\n", 1)[1])
            return {"scenes": [{"n": it["n"], "dialogue_es": "es"} for it in items]}
        if content.startswith("Del siguiente guion"):
            return {"keywords": ["ginger", "free", "three"]}
    else:
        txt = content[-1]["text"]
        if txt.startswith("META-PROMPT"):
            close = ("Use the provided character image for the person's appearance, face, and clothing exactly as shown."
                     if txt.startswith("META-PROMPT 1") else
                     "Use Image A for the specific action and Image B for the character appearance and environment continuity.")
            return {"image_prompt": "Vertical 9:16 shot. " + close, "action": "Points at the chest"}
        if txt.startswith("Analiza a la persona"):
            return {"description": "a man", "gender": "male", "age_range": "35-45",
                    "voice": {"gender": "male", "age": "middle_aged", "tone": "warm"}, "summary_es": "ok"}
        if txt.startswith("Te muestro"):
            nums = [int(x) for x in re.findall(r"ESCENA (\d+)", " ".join(b["text"] for b in content if b["type"] == "text"))]
            return {"scenes": [{"n": k, "shot": "medium", "person": "talks", "setting": "kitchen with US flag", "props": [],
                                "on_screen_text": "", "lighting": "warm", "motion": "none", "summary_es": "cocina"} for k in nums]}
        if "Para CADA clip" in txt:
            items = json.loads(txt.split("\n\n", 1)[1])
            return {"scenes": [{"scene": it["scene"], "clips": [
                {"clip": c["clip"], "camera": "Medium shot.", "action_en": "Smiles and gestures.", "delivery": "warm",
                 "action_es": "sonrie", "dialogue_es": "ES " + c["dialogue_en"]} for c in it["clips"]]} for it in items]}
        if txt.startswith("Compara 3 imagenes"):
            return {"same_person_as_avatar": True, "same_outfit_as_avatar": True, "is_copy_of_original_person": False,
                    "action_matches_original": True, "differences": "", "fix": ""}
        if txt.startswith("ESTADO"):            # supervisor: acepta todo lo pendiente, reintenta lo fallido
            state = json.loads(txt.split("\n", 1)[1].split("\n\nEVENTOS")[0])
            acts = [{"do": "retry", "scene": r["scene"], "clip": r["clip"], "reason": "error"} for r in state if r["state"] == "failed"]
            pend = txt.split("CLIPS PENDIENTES DE TU REVISION")[1].split("\n")[0]
            acts += [{"do": "accept", "scene": int(a), "clip": int(b)} for a, b in re.findall(r"E(\d+)C(\d+)", pend)]
            return {"analysis": "Demo: los clips pasan la auditoria.", "actions": acts}
    raise AssertionError("prompt inesperado: " + str(content)[:200])


def fake_transcribe(audio, settings=None, language_code=None):
    d = media.probe(audio)["duration"]
    words = SCRIPT.split()
    step = d / len(words)
    return {"language": "en", "text": SCRIPT, "words": [
        {"text": w, "start": i * step, "end": i * step + step * 0.8} for i, w in enumerate(words)]}


VOICES = [
    {"voice_id": "v1", "name": "Ana", "category": "premade", "gender": "female", "age": "young", "accent": "american",
     "descriptive": "warm", "use_case": "social media", "preview_url": None},
    {"voice_id": "v2", "name": "Bob", "category": "premade", "gender": "male", "age": "middle aged", "accent": "american",
     "descriptive": "warm", "use_case": "conversational", "preview_url": None}]


def install() -> None:
    """Sustituye los servicios externos por versiones simuladas."""
    from .services import claude, dubvoice, eleven, gemini, google_veo, kie, stt
    for k in ("ELEVENLABS_API_KEY", "KIE_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "DUBVOICE_API_KEY"):
        os.environ[k] = "demo"
    claude.ask_json = fake_ask_json
    stt.transcribe = fake_transcribe
    eleven.list_voices = lambda: list(VOICES)
    dubvoice.list_voices = lambda gender=None, language="en", n=40: [v for v in VOICES if not gender or v["gender"] == gender]
    dubvoice.voice_change = lambda url, vid, progress=None, audio_path=None: mp3(8)
    kie.upload_file = lambda path, mime="audio/mpeg", folder="": "https://example.com/a.mp3"
    kie.upload_image = lambda path: "https://example.com/x.jpg"
    gemini.generate_image = lambda prompt, refs, model, aspect="9:16": jpg()
    dubvoice.image = lambda prompt, refs, model="nano-banana-2", aspect="9:16", progress=None: jpg()
    google_veo.veo = (lambda prompt, image_path, model="x", aspect="9:16", duration=8, resolution="720p", progress=None,
                      timeout=0, cancel=None: ("google-task", fake_video(prompt)))
    dubvoice.veo = (lambda prompt, image_path, model="veo-3.1-fast", aspect="9:16", resolution="720p", progress=None,
                    timeout=0, duration=None, cancel=None: ("demo-task", fake_video(prompt)))

"""ElevenLabs: transcripcion (Scribe), catalogo de voces y speech-to-speech."""
from __future__ import annotations

from pathlib import Path

from ..config import require_key
from .http import fail, request

BASE = "https://api.elevenlabs.io"


def _h() -> dict:
    return {"xi-api-key": require_key("elevenlabs")}


def transcribe(audio: Path, language_code: str | None = None) -> dict:
    """-> {language, text, words:[{text,start,end}]} con marcas de tiempo por palabra."""
    data = {"model_id": "scribe_v1", "timestamps_granularity": "word",
            "diarize": "false", "tag_audio_events": "false"}
    if language_code:
        data["language_code"] = language_code
    with open(audio, "rb") as fh:
        r = request("POST", f"{BASE}/v1/speech-to-text", headers=_h(), data=data,
                    files={"file": (audio.name, fh.read())}, timeout=900)
    if r.status_code != 200:
        raise fail("ElevenLabs (transcripcion)", r)
    j = r.json()
    words = [{"text": w["text"].strip(), "start": float(w["start"]), "end": float(w["end"])}
             for w in j.get("words", []) if w.get("type", "word") == "word" and w.get("text", "").strip()]
    return {"language": j.get("language_code"), "text": j.get("text", "").strip(), "words": words}


def list_voices() -> list[dict]:
    r = request("GET", f"{BASE}/v1/voices", headers=_h(), timeout=60)
    if r.status_code != 200:
        raise fail("ElevenLabs (voces)", r)
    out = []
    for v in r.json().get("voices", []):
        lab = v.get("labels") or {}
        out.append({"voice_id": v["voice_id"], "name": v.get("name"), "category": v.get("category"),
                    "gender": (lab.get("gender") or "").lower(), "age": (lab.get("age") or "").lower(),
                    "accent": (lab.get("accent") or "").lower(),
                    "descriptive": (lab.get("descriptive") or lab.get("description") or "").lower(),
                    "use_case": (lab.get("use_case") or lab.get("usecase") or "").lower(),
                    "preview_url": v.get("preview_url")})
    return out


def speech_to_speech(audio: Path, voice_id: str) -> bytes:
    """Cambia la voz conservando tiempos/entonacion (mantiene el lipsync del clip)."""
    with open(audio, "rb") as fh:
        r = request("POST", f"{BASE}/v1/speech-to-speech/{voice_id}", headers=_h(),
                    data={"model_id": "eleven_multilingual_sts_v2", "remove_background_noise": "true",
                          "output_format": "mp3_44100_128"},
                    files={"audio": (audio.name, fh.read())}, timeout=600)
    if r.status_code != 200:
        raise fail("ElevenLabs (speech-to-speech)", r)
    return r.content

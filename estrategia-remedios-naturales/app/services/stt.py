"""Transcripcion con marcas por palabra: local (faster-whisper, gratis) o ElevenLabs Scribe."""
from __future__ import annotations

from pathlib import Path

from . import eleven

_models: dict = {}


def transcribe_local(audio: Path, language: str | None = "en", model_name: str = "small.en") -> dict:
    try:
        from faster_whisper import WhisperModel
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"faster-whisper no carga ({type(e).__name__}: {e}). Instala las dependencias con "
                           "`.venv/bin/python -m pip install -r requirements.txt` o cambia la transcripcion a ElevenLabs en la Fase 2.")
    if model_name not in _models:  # la primera vez descarga el modelo (~250 MB)
        _models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    lang = None if model_name.endswith(".en") else language   # los modelos .en solo hablan ingles
    segs, info = _models[model_name].transcribe(str(audio), language=lang, word_timestamps=True,
                                                vad_filter=True, beam_size=5)
    words = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append({"text": t, "start": float(w.start), "end": float(w.end)})
    return {"language": getattr(info, "language", language), "text": " ".join(w["text"] for w in words), "words": words}


def transcribe(audio: Path, settings: dict, language_code: str | None = None) -> dict:
    if settings.get("stt_provider") == "elevenlabs":
        return eleven.transcribe(audio, language_code)
    model = settings.get("whisper_model", "small.en")
    if language_code and language_code != "en" and model.endswith(".en"):
        model = model[:-3]          # los modelos ".en" solo entienden ingles: usar el multilingue
    return transcribe_local(audio, language_code or "en", model)

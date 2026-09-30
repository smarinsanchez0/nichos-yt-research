"""FASE 5: generacion de los clips de video (Veo via Kie.ai) + voz unificada (ElevenLabs)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .. import media, store
from ..services import dubvoice, eleven, kie
from .common import abs_path


# ----------------------------------------------------------------- voces
def score_voice(v: dict, want: dict) -> int:
    s = 0
    g = (want.get("gender") or "").lower()
    if g and v["gender"] == g:
        s += 6
    elif g and v["gender"] and v["gender"] != g:
        s -= 10
    age = (want.get("age") or "").replace("_", " ")
    if age and age in v["age"].replace("_", " "):
        s += 3
    if "american" in v["accent"] or "us" == v["accent"]:
        s += 2
    if any(k in v["use_case"] for k in ("social", "conversational", "narration", "advertisement")):
        s += 2
    tone = (want.get("tone") or "").lower()
    s += sum(1 for w in v["descriptive"].replace(",", " ").split() if w and w in tone)
    if v["category"] in ("premade", "professional", "generated"):
        s += 1
    return s


def recommend(profile: dict | None, voices: list[dict]) -> list[dict]:
    want = (profile or {}).get("voice") or {}
    ranked = sorted(voices, key=lambda v: score_voice(v, want), reverse=True)
    return [{**v, "score": score_voice(v, want)} for v in ranked]


# ----------------------------------------------------------------- clips
def _unify(pid: str, si: int, ci: int, raw, st: dict, clip: dict):
    """Cambia la voz del clip a la voz elegida. Devuelve (archivo_final, aviso)."""
    if not (st.get("unify_voice") and st.get("voice_id") and clip.get("dialogue") and media.probe(raw)["has_audio"]):
        return raw, None
    try:
        aud = media.extract_audio(raw, store.path(pid, "videos", f"s{si:02d}_c{ci}_src.mp3"))
        if st.get("voice_provider") == "elevenlabs":
            new = eleven.speech_to_speech(aud, st["voice_id"])
        else:
            try:
                url = kie.upload_file(aud, "audio/mpeg")
            except Exception:  # noqa: BLE001  - sin URL publica se usa la subida directa
                url = None
            new = dubvoice.voice_change(url, st["voice_id"], audio_path=aud)
        mp3 = store.path(pid, "videos", f"s{si:02d}_c{ci}_voice.mp3")
        mp3.write_bytes(new)
        final = store.path(pid, "videos", f"s{si:02d}_c{ci}.mp4")
        media.mux_audio(raw, mp3, final)
        return final, None
    except Exception as e:  # noqa: BLE001  - se conserva la voz original de Veo
        return raw, f"No se pudo unificar la voz ({str(e)[:300]}). Se conserva la voz de Veo."


def retry_voice(pid: str, si: int, ci: int) -> str | None:
    """Reintenta solo el cambio de voz sobre el clip ya generado. Devuelve el aviso (None si salio bien)."""
    p = store.get(pid)
    clip = p["scenes"][si]["clips"][ci]
    if not clip.get("raw"):
        raise RuntimeError("Ese clip aun no esta generado.")
    raw = abs_path(pid, clip["raw"])
    final, warning = _unify(pid, si, ci, raw, p["settings"], clip)
    with store.edit(pid) as q:
        c = q["scenes"][si]["clips"][ci]
        c.update(file=store.rel(pid, final), warning=warning, duration=media.probe(final)["duration"])
    return warning


def render_clip(pid: str, si: int, ci: int, prog=None, cancel=None, overrides: dict | None = None) -> None:
    ov = overrides or {}
    p = store.get(pid)
    st = p["settings"]
    clip = p["scenes"][si]["clips"][ci]
    if not (p["scenes"][si].get("image") or {}).get("approved"):
        raise RuntimeError(f"La imagen de la escena {si + 1} no esta aprobada.")
    model = ov.get("model") or st["dubvoice_video_model"]
    duration = ov.get("duration") or clip.get("target")
    prompt = ov.get("prompt") or clip["video_prompt"]
    with store.edit(pid) as q:
        c = q["scenes"][si]["clips"][ci]
        c.update(status="running", error=None, warning=None, audit=None, verified=None)
        if ov.get("prompt"):
            c["video_prompt"] = ov["prompt"]
    try:
        pg = (lambda m: prog(m)) if prog else None
        task, data = dubvoice.veo(prompt, abs_path(pid, p["scenes"][si]["image"]["file"]), model=model, progress=pg,
                                  duration=duration, cancel=cancel)
        raw = store.path(pid, "videos", f"s{si:02d}_c{ci}_raw.mp4")
        raw.write_bytes(data)
        final, warning = _unify(pid, si, ci, raw, st, clip)
        info = media.probe(final)
        with store.edit(pid) as q:
            c = q["scenes"][si]["clips"][ci]
            c.update(status="done", file=store.rel(pid, final), raw=store.rel(pid, raw), task_id=task, model_used=model,
                     asked_seconds=dubvoice.tier_for(duration or 8, model),
                     duration=info["duration"], stale=False, warning=warning, error=None)
    except Exception as e:  # noqa: BLE001
        with store.edit(pid) as q:
            q["scenes"][si]["clips"][ci].update(status="error", error=str(e)[:600])
        raise


def render_many(pid: str, prog, pairs: list[tuple[int, int]]) -> None:
    done = [0]
    errors: list[str] = []

    def one(pair):
        try:
            render_clip(pid, *pair)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Escena {pair[0] + 1} clip {pair[1] + 1}: {e}")
        done[0] += 1
        prog(f"Clips listos {done[0]}/{len(pairs)}", done[0] / len(pairs))

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, pairs))
    if errors:
        raise RuntimeError(" | ".join(errors)[:900])


def estimate(p: dict) -> dict:
    """Creditos estimados de DubVoice segun la duracion que pedira cada clip vs. 8 s fijos."""
    st = p["settings"]
    model = st["dubvoice_video_model"]
    rows, total, fixed = [], 0, 0
    for s in p["scenes"]:
        for c in s.get("clips", []):
            t = float(c.get("target") or 8)
            secs = dubvoice.tier_for(t, model)
            cr = dubvoice.credits_for(model, t)
            rows.append({"scene": s["idx"], "clip": c["idx"], "needed": round(t, 1), "asked": secs, "credits": cr})
            total += cr or 0
            fixed += dubvoice.CREDITS.get("veo-3.1-fast", 7500)
    return {"clips": rows, "total_credits": total, "veo_fast_credits": fixed,
            "variable": model == "omniflash"}

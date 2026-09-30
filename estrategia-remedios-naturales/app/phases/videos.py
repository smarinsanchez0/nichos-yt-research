"""FASE 5: generacion de los clips de video (Veo via Kie.ai) + voz unificada (ElevenLabs)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .. import media, store
from ..services import dubvoice, eleven, google_veo, kie
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
    provider = ov.get("provider") or "dubvoice"
    model = ov.get("model") or (st.get("google_video_model") or google_veo.DEFAULT_MODEL if provider == "google" else st["dubvoice_video_model"])
    duration = ov.get("duration") or clip.get("target")
    prompt = ov.get("prompt") or clip["video_prompt"]
    with store.edit(pid) as q:
        c = q["scenes"][si]["clips"][ci]
        c.update(status="running", error=None, warning=None, audit=None, verified=None)
        if ov.get("prompt"):
            c["video_prompt"] = ov["prompt"]
    try:
        pg = (lambda m: prog(m)) if prog else None
        img = abs_path(pid, p["scenes"][si]["image"]["file"])
        if provider == "google":
            task, data = google_veo.veo(prompt, img, model=model, progress=pg, duration=duration, cancel=cancel)
        else:
            task, data = dubvoice.veo(prompt, img, model=model, progress=pg, duration=duration, cancel=cancel)
        raw = store.path(pid, "videos", f"s{si:02d}_c{ci}_raw.mp4")
        raw.write_bytes(data)
        final, warning = _unify(pid, si, ci, raw, st, clip)
        info = media.probe(final)
        with store.edit(pid) as q:
            c = q["scenes"][si]["clips"][ci]
            c.update(status="done", file=store.rel(pid, final), raw=store.rel(pid, raw), task_id=task, model_used=model,
                     provider_used=provider,
                     asked_seconds=(google_veo.pick_duration(duration) if provider == "google" else dubvoice.tier_for(duration or 8, model)),
                     duration=info["duration"], stale=False, warning=warning, error=None)
    except Exception as e:  # noqa: BLE001
        with store.edit(pid) as q:
            q["scenes"][si]["clips"][ci].update(status="error", error=str(e)[:600])
        raise


def make_still_clip(pid: str, si: int, ci: int) -> None:
    """Salida garantizada: locucion (misma voz elegida) sobre la imagen aprobada con un zoom suave. No depende de ningun proveedor de video."""
    p = store.get(pid)
    st = p["settings"]
    scene = p["scenes"][si]
    c = scene["clips"][ci]
    lang = st.get("output_language", "es")
    text = (c.get("dialogue") or "").strip()
    out = store.path(pid, "videos", f"s{si:02d}_c{ci}_still.mp4")
    img = abs_path(pid, scene["image"]["file"])
    secs = float(c.get("target") or 6)
    audio = None
    if text:
        if st.get("voice_id"):
            data = dubvoice.tts(text, st["voice_id"], lang)
        else:
            data = dubvoice.edge_tts(text, "es-MX-JorgeNeural" if lang == "es" else "en-US-GuyNeural")
        audio = store.path(pid, "videos", f"s{si:02d}_c{ci}_tts.mp3")
        audio.write_bytes(data)
        secs = max(media.probe(audio)["duration"] + 0.3, 1.5)
    frames = int(secs * 30)
    vf = ("scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,"
          f"zoompan=z='min(zoom+0.0007,1.14)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps=30,format=yuv420p")
    args = ["-i", str(img)]
    args += ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-vf", vf, "-t", f"{secs:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-shortest", str(out)]
    media.run(args)
    with store.edit(pid) as q:
        q["scenes"][si]["clips"][ci].update(
            status="done", file=store.rel(pid, out), raw=store.rel(pid, out), duration=media.probe(out)["duration"], stale=False,
            provider_used="still", verified=True, error=None,
            warning="Locucion sobre imagen fija (no se pudo generar el video de este clip). Regeneralo cuando haya proveedor disponible.")


def salvage_all(pid: str, prog) -> None:
    p = store.get(pid)
    todo = [(s["idx"], c["idx"]) for s in p["scenes"] for c in s["clips"] if c.get("status") != "done" or c.get("stale")]
    for n, (si, ci) in enumerate(todo):
        prog(f"Locucion sobre imagen: escena {si + 1} clip {ci + 1} ({n + 1}/{len(todo)})", n / max(len(todo), 1))
        make_still_clip(pid, si, ci)


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

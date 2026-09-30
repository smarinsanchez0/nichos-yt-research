"""FASE 5: generacion de los clips de video (Veo via Kie.ai) + voz unificada (ElevenLabs)."""
from __future__ import annotations

import inspect

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
        if isinstance(c.get("f5"), dict):        # el audio se repara SIN regenerar el video (F5: audio_state independiente del visual)
            c["f5"]["audio_state"] = "NEEDS_FIX" if warning else "OK"
            c["f5"]["audio_issue"] = warning
    return warning


def _call(fn, args: tuple, kw: dict, extras: dict):
    """Llama al proveedor pasando los parametros nuevos de F5 (on_submit, limits, ...) SOLO si la funcion los acepta."""
    try:
        params = inspect.signature(fn).parameters
        var_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params, var_kw = {}, True
    ok = {k: v for k, v in extras.items() if v is not None and (var_kw or k in params)}
    return fn(*args, **kw, **ok)


def default_model(st: dict, provider: str) -> str:
    return (st.get("google_video_model") or google_veo.DEFAULT_MODEL) if provider == "google" else st["dubvoice_video_model"]


def generate_raw(pid: str, si: int, ci: int, *, provider: str, model: str | None, prompt: str, duration: float | None,
                 cancel=None, progress=None, on_submit=None, on_poll=None, limits: dict | None = None, gate=None,
                 recorder=None) -> tuple[str, bytes]:
    """A + B de la Fase 5: ENVIAR → SONDEAR → DESCARGAR. Devuelve (job_id, bytes del MP4). No toca voz ni auditoria:
    en cuanto esta funcion vuelve, el slot de generacion puede liberarse."""
    p = store.get(pid)
    img = abs_path(pid, p["scenes"][si]["image"]["file"])
    model = model or default_model(p["settings"], provider)
    fn = google_veo.veo if provider == "google" else dubvoice.veo
    return _call(fn, (prompt, img), {"model": model, "progress": progress, "duration": duration, "cancel": cancel},
                 {"on_submit": on_submit, "on_poll": on_poll, "limits": limits, "gate": gate, "recorder": recorder})


def resume_raw(provider: str, job_id: str, *, model: str | None = None, cancel=None, progress=None, on_poll=None,
               limits: dict | None = None, gate=None, recorder=None, result_url: str | None = None) -> tuple[str, bytes]:
    """Reconciliacion: sondea un job YA existente y descarga su resultado. NUNCA crea otro job (cero POST de creacion)."""
    fn = getattr(google_veo if provider == "google" else dubvoice, "resume", None)
    if fn is None:
        raise RuntimeError(f"{provider}: no se puede reanudar un job existente (resume no disponible)")
    return _call(fn, (job_id,), {"progress": progress, "cancel": cancel},
                 {"model": model, "on_poll": on_poll, "limits": limits, "gate": gate, "recorder": recorder, "result_url": result_url})


def list_candidates(provider: str, *, since: float, until: float, model: str | None = None, limits: dict | None = None, gate=None,
                    cancel=None, recorder=None) -> list[dict]:
    """Reconciliacion de un POST ambiguo: generaciones remotas recientes (SOLO LECTURA). NotSupported si el proveedor no lo ofrece."""
    from ..services.errors import NotSupported
    fn = getattr(dubvoice, "list_generations", None) if provider == "dubvoice" else None
    if fn is None:
        raise NotSupported(f"{provider}: no hay forma de listar generaciones para reconciliar un envio ambiguo")
    return _call(fn, (), {"since": since, "until": until, "model": model}, {"limits": limits, "gate": gate, "cancel": cancel, "recorder": recorder})


def finish_clip(pid: str, si: int, ci: int, raw) -> tuple:
    """C de la Fase 5: unifica la voz (si esta activado). Devuelve (archivo_final, aviso). Ante cualquier fallo devuelve el RAW
    y un aviso: el RAW pagado nunca se pierde ni obliga a regenerar el video."""
    p = store.get(pid)
    return _unify(pid, si, ci, raw, p["settings"], p["scenes"][si]["clips"][ci])


def render_clip(pid: str, si: int, ci: int, prog=None, cancel=None, overrides: dict | None = None) -> None:
    """Camino heredado y sincrono (A+B+C en una llamada, sin maquina de estados). F5 usa video_jobs; esto queda para uso manual."""
    ov = overrides or {}
    p = store.get(pid)
    st = p["settings"]
    clip = p["scenes"][si]["clips"][ci]
    if not (p["scenes"][si].get("image") or {}).get("approved"):
        raise RuntimeError(f"La imagen de la escena {si + 1} no esta aprobada.")
    provider = ov.get("provider") or "dubvoice"
    model = ov.get("model") or default_model(st, provider)
    duration = ov.get("duration") or clip.get("target")
    prompt = ov.get("prompt") or clip["video_prompt"]
    with store.edit(pid) as q:
        c = q["scenes"][si]["clips"][ci]
        c.update(status="running", error=None, warning=None, audit=None, verified=None)
        if ov.get("prompt"):
            c["video_prompt"] = ov["prompt"]
    try:
        pg = (lambda m: prog(m)) if prog else None
        task, data = generate_raw(pid, si, ci, provider=provider, model=model, prompt=prompt, duration=duration, cancel=cancel, progress=pg)
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
        cl = q["scenes"][si]["clips"][ci]
        if isinstance(cl.get("f5"), dict):        # OPT-IN explicito del usuario (nunca fallback automatico): queda registrado como tal
            cl["f5"].update(state="ACCEPTED", visual_state="OK_STILL_IMAGE", accepted_via="still_image_optin", review_reason=None,
                            review_kind=None, review_message=None)


def salvage_all(pid: str, prog) -> None:
    p = store.get(pid)
    todo = [(s["idx"], c["idx"]) for s in p["scenes"] for c in s["clips"] if c.get("status") != "done" or c.get("stale")]
    for n, (si, ci) in enumerate(todo):
        prog(f"Locucion sobre imagen: escena {si + 1} clip {ci + 1} ({n + 1}/{len(todo)})", n / max(len(todo), 1))
        make_still_clip(pid, si, ci)


def render_many(pid: str, prog, pairs: list[tuple[int, int]], explicit: bool = False) -> None:
    """Punto de entrada manual: delega en el planificador unico de F5 (video_jobs). No crea hilos propios."""
    from . import video_jobs
    video_jobs.run_project(pid, pairs, prog=prog, explicit=explicit)


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

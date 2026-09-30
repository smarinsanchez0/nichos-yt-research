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
def _image_url(pid: str, si: int) -> str:
    p = store.get(pid)
    s = p["scenes"][si]
    cached = s.get("image_url")
    if cached and cached.get("for") == s["image"]["file"]:
        return cached["url"]
    url = kie.upload_image(abs_path(pid, s["image"]["file"]))
    with store.edit(pid) as q:
        q["scenes"][si]["image_url"] = {"for": s["image"]["file"], "url": url}
    return url


def render_clip(pid: str, si: int, ci: int, prog=None) -> None:
    p = store.get(pid)
    st = p["settings"]
    clip = p["scenes"][si]["clips"][ci]
    if not (p["scenes"][si].get("image") or {}).get("approved"):
        raise RuntimeError(f"La imagen de la escena {si + 1} no esta aprobada.")
    with store.edit(pid) as q:
        c = q["scenes"][si]["clips"][ci]
        c.update(status="running", error=None, warning=None)
    try:
        pg = (lambda m: prog(m)) if prog else None
        if st.get("video_provider") == "dubvoice":
            task, data = dubvoice.veo(clip["video_prompt"], abs_path(pid, p["scenes"][si]["image"]["file"]),
                                      model=st["dubvoice_video_model"], progress=pg)
        else:
            task, data = kie.veo_generate(clip["video_prompt"], _image_url(pid, si), model=st["video_model"], progress=pg)
        raw = store.path(pid, "videos", f"s{si:02d}_c{ci}_raw.mp4")
        raw.write_bytes(data)
        final, warning = raw, None
        if st.get("unify_voice") and st.get("voice_id") and clip.get("dialogue") and media.probe(raw)["has_audio"]:
            try:
                aud = media.extract_audio(raw, store.path(pid, "videos", f"s{si:02d}_c{ci}_src.mp3"))
                new = eleven.speech_to_speech(aud, st["voice_id"])
                mp3 = store.path(pid, "videos", f"s{si:02d}_c{ci}_voice.mp3")
                mp3.write_bytes(new)
                final = store.path(pid, "videos", f"s{si:02d}_c{ci}.mp4")
                media.mux_audio(raw, mp3, final)
            except Exception as e:  # noqa: BLE001  - se conserva la voz original de Veo
                final, warning = raw, f"No se pudo unificar la voz ({e}). Se conserva la voz de Veo."
        info = media.probe(final)
        with store.edit(pid) as q:
            c = q["scenes"][si]["clips"][ci]
            c.update(status="done", file=store.rel(pid, final), raw=store.rel(pid, raw), task_id=task,
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

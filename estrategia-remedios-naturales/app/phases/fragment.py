"""FASE 4: fragmentacion del guion por escena/imagen y prompts de video (Veo)."""
from __future__ import annotations

import json
import re

from .. import store
from ..services import claude
from .common import abs_path, assign_words

MAX_SPEECH = {"en": 6.8, "es": 5.6}   # s de habla (en el original) por clip; Veo genera 8 s y el español dura ~20% mas
MAX_WORDS = {"en": 22, "es": 17}
SUFFIX = ("Photorealistic, natural smartphone-style footage, the face, hair and outfit stay identical to the first frame, "
          "realistic lip-sync and natural body motion. No subtitles, no captions, no on-screen text, no watermark, no music.")


def chunk_words(words: list[dict], lang: str = "en") -> list[list[dict]]:
    """Parte las palabras en trozos que caben en un clip, cortando en puntuacion cuando se puede."""
    max_speech, max_words = MAX_SPEECH[lang], MAX_WORDS[lang]
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if cur[-1]["end"] - cur[0]["start"] > max_speech or len(cur) > max_words:
            cut = None
            for j in range(len(cur) - 2, len(cur) // 2 - 1, -1):
                if re.search(r"[.!?,;:…]$", cur[j]["text"]):
                    cut = j + 1
                    break
            if cut is None:
                cut = len(cur) - 1
            chunks.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        if chunks and len(cur) < 3 and cur[-1]["end"] - chunks[-1][0]["start"] <= max_speech + 1.0:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    return chunks


def build_prompt(c: dict) -> str:
    parts = [c.get("camera", "").strip(), c.get("action_en", "").strip()]
    if c.get("dialogue"):
        if c.get("lang") == "es":
            parts.append(f'The person speaks directly to the camera in Spanish, with a {c.get("delivery") or "warm, trustworthy"} '
                         f'neutral Latin American Spanish voice, and says in Spanish: "{c["dialogue"]}"')
        else:
            parts.append(f'The person speaks directly with a {c.get("delivery") or "warm, trustworthy"} American English voice and says: '
                         f'"{c["dialogue"]}"')
    else:
        parts.append("The person does not speak in this shot; only natural ambient sound.")
    parts.append(SUFFIX)
    return " ".join(x for x in parts if x)


SYSTEM = """Eres director de video para Instagram Reels de venta (nicho remedios naturales, publico de EE.UU.).
Escribes indicaciones para Veo 3 (imagen a video con voz). Recibes la imagen inicial de cada escena, lo que ocurre en el
video ORIGINAL en esa escena y el dialogo exacto de cada clip. Tu trabajo: describir la ACCION y la camara de cada clip para que el
avatar haga lo mismo que la persona original (gestos, mirada, objetos que muestra) y que la entrega del dialogo suene natural:
educativa, cercana y persuasiva. No cambies el dialogo. Cuando el video final es en español, ademas TRADUCES el dialogo de cada clip del ingles al español
hablado natural (mismo significado y tono de venta, frases cortas, sin inventar promesas), con un largo similar para que quepa en ~7 segundos."""


def run(pid: str, prog) -> None:
    p = store.get(pid)
    tr = (p["analysis"].get("transcript") or {}).get("words")
    scenes = p["scenes"]
    if not scenes:
        raise RuntimeError("Completa la Fase 2 primero.")
    if not all((s.get("image") or {}).get("approved") for s in scenes):
        raise RuntimeError("Aprueba todas las imagenes de la Fase 3 antes de fragmentar.")
    lang = p["settings"].get("output_language", "es")
    groups = assign_words(tr or [], scenes)
    plan = []          # por escena: lista de clips base
    for s, ws in zip(scenes, groups):
        dur = s["end"] - s["start"]
        chunks = chunk_words(ws, lang)
        clips = []
        if not chunks:
            clips.append({"dialogue": "", "dialogue_en": "", "lang": lang, "t_start": s["start"], "t_end": s["end"], "target": min(dur, 8.0)})
        for k, ch in enumerate(chunks):
            sp = ch[-1]["end"] - ch[0]["start"]
            target = sp + 0.7
            if len(chunks) == 1:
                target = max(target, dur)
            clips.append({"dialogue": " ".join(w["text"] for w in ch), "dialogue_en": " ".join(w["text"] for w in ch),
                          "lang": lang, "t_start": ch[0]["start"],
                          "t_end": ch[-1]["end"], "words": [ws.index(ch[0]), ws.index(ch[-1])],
                          "target": round(min(target, 8.0), 2)})
        plan.append(clips)

    B = 5
    profile = (p["avatar"] or {}).get("profile") or {}
    for a in range(0, len(scenes), B):
        batch = list(range(a, min(a + B, len(scenes))))
        prog(f"Redactando prompts de video ({a + 1}-{batch[-1] + 1} de {len(scenes)})…", a / len(scenes))
        content = []
        items = []
        for i in batch:
            s = scenes[i]
            content.append({"type": "text", "text": f"Imagen inicial de la ESCENA {i + 1}:"})
            content.append(claude.image_block(abs_path(pid, s["image"]["file"]), max_side=512))
            items.append({"scene": i + 1, "original_scene": s["read"],
                          "clips": [{"clip": k + 1, "dialogue_en": c["dialogue_en"],
                                     "max_words_es": min(24, int(len(c["dialogue_en"].split()) * 1.2) + 1)} for k, c in enumerate(plan[i])]})
        content.append({"type": "text", "text": (
            f"Voz/personaje: {json.dumps(profile.get('voice', {}), ensure_ascii=False)}. Notas globales: {p['settings'].get('global_notes') or '-'}\n"
            "Para CADA clip devuelve: "
            '{"scenes":[{"scene":<n>,"clips":[{"clip":<k>,"camera":"plano/movimiento de camara en ingles","action_en":"accion del avatar en ingles, concreta (mirada, gestos, objetos)",'
            '"delivery":"tono de voz en 3-5 palabras en ingles","action_es":"accion en español, una frase",'
            '"dialogue_es":"traduccion al español hablado del dialogue_en (vacio si no hay dialogo), maximo max_words_es palabras"}]}]}\n\n'
            + json.dumps(items, ensure_ascii=False))})
        data = claude.ask_json(content, system=SYSTEM, model=p["settings"]["claude_model"], max_tokens=6000)
        by = {(int(x["scene"]), int(c["clip"])): c for x in data["scenes"] for c in x["clips"]}
        for i in batch:
            for k, c in enumerate(plan[i]):
                r = by.get((i + 1, k + 1))
                if not r:
                    raise RuntimeError(f"Falto el prompt del clip {k + 1} de la escena {i + 1}; reintenta.")
                c.update(camera=r.get("camera", ""), action_en=r.get("action_en", ""), delivery=r.get("delivery", ""),
                         action_es=r.get("action_es", ""))
                if lang == "es" and c["dialogue_en"]:
                    es = (r.get("dialogue_es") or "").strip()
                    if not es:
                        raise RuntimeError(f"Falto la traduccion del clip {k + 1} de la escena {i + 1}; reintenta.")
                    c["dialogue"] = es
                c["video_prompt"] = build_prompt(c)
                c.update(idx=k, status="pending", error=None, file=None, stale=False)
    with store.edit(pid) as q:
        for i, clips in enumerate(plan):
            q["scenes"][i]["clips"] = clips
        q["final"] = None


def update_clip(pid: str, si: int, ci: int, fields: dict) -> None:
    with store.edit(pid) as q:
        c = q["scenes"][si]["clips"][ci]
        changed_parts = False
        for k in ("dialogue", "camera", "action_en", "delivery", "action_es"):
            if k in fields and fields[k] != c.get(k):
                c[k] = fields[k]
                changed_parts = True
        if "video_prompt" in fields and fields["video_prompt"] != c.get("video_prompt"):
            c["video_prompt"] = fields["video_prompt"]
        elif changed_parts:
            c["video_prompt"] = build_prompt(c)
        if "video_prompt" in fields or changed_parts:
            c["stale"] = True

"""FASE 4: fragmentacion del guion por escena/imagen y prompts de video (Veo)."""
from __future__ import annotations

import json
import re

from .. import store
from ..services import claude
from .common import abs_path, assign_words

MAX_SPEECH = 6.8   # s de habla por clip (Veo genera 8 s)
MAX_WORDS = 22
SUFFIX = ("Photorealistic, natural smartphone-style footage, the face, hair and outfit stay identical to the first frame, "
          "realistic lip-sync and natural body motion. No subtitles, no captions, no on-screen text, no watermark, no music.")


def chunk_words(words: list[dict]) -> list[list[dict]]:
    """Parte las palabras en trozos que caben en un clip, cortando en puntuacion cuando se puede."""
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if cur[-1]["end"] - cur[0]["start"] > MAX_SPEECH or len(cur) > MAX_WORDS:
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
        if chunks and len(cur) < 3 and cur[-1]["end"] - chunks[-1][0]["start"] <= MAX_SPEECH + 1.0:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    return chunks


def build_prompt(c: dict) -> str:
    parts = [c.get("camera", "").strip(), c.get("action_en", "").strip()]
    if c.get("dialogue"):
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
educativa, cercana y persuasiva. No cambies el dialogo."""


def run(pid: str, prog) -> None:
    p = store.get(pid)
    tr = (p["analysis"].get("transcript") or {}).get("words")
    scenes = p["scenes"]
    if not scenes:
        raise RuntimeError("Completa la Fase 2 primero.")
    if not all((s.get("image") or {}).get("approved") for s in scenes):
        raise RuntimeError("Aprueba todas las imagenes de la Fase 3 antes de fragmentar.")
    groups = assign_words(tr or [], scenes)
    plan = []          # por escena: lista de clips base
    for s, ws in zip(scenes, groups):
        dur = s["end"] - s["start"]
        chunks = chunk_words(ws)
        clips = []
        if not chunks:
            clips.append({"dialogue": "", "t_start": s["start"], "t_end": s["end"], "target": min(dur, 8.0)})
        for k, ch in enumerate(chunks):
            sp = ch[-1]["end"] - ch[0]["start"]
            target = sp + 0.7
            if len(chunks) == 1:
                target = max(target, dur)
            clips.append({"dialogue": " ".join(w["text"] for w in ch), "t_start": ch[0]["start"],
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
                          "clips": [{"clip": k + 1, "dialogue": c["dialogue"]} for k, c in enumerate(plan[i])]})
        content.append({"type": "text", "text": (
            f"Voz/personaje: {json.dumps(profile.get('voice', {}), ensure_ascii=False)}. Notas globales: {p['settings'].get('global_notes') or '-'}\n"
            "Para CADA clip devuelve: "
            '{"scenes":[{"scene":<n>,"clips":[{"clip":<k>,"camera":"plano/movimiento de camara en ingles","action_en":"accion del avatar en ingles, concreta (mirada, gestos, objetos)",'
            '"delivery":"tono de voz en 3-5 palabras en ingles","action_es":"accion en español, una frase"}]}]}\n\n'
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

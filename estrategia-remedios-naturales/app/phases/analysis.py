"""FASE 2: transcripcion, traduccion, escenas/frames y prompts de imagen."""
from __future__ import annotations

import time

from .. import media, store
from ..services import claude, stt
from .common import abs_path, assign_words, make_segments

SYSTEM_BASE = """Trabajas en un equipo que replica videos virales (Instagram Reels, 9:16) que venden un libro digital
de recetas de remedios naturales a latinos en EE.UU. y otros paises de habla inglesa. Los videos tienen
estetica estadounidense (cocinas americanas, banderas de EE.UU. de fondo o al lado, hogares tipicos).
Debes ser EXTREMADAMENTE fiel a lo que se ve y se dice en el original: no inventes ni embellezcas."""


def _model(p):
    return p["settings"]["claude_model"]


def _step_transcription(pid, prog):
    p = store.get(pid)
    src = p["source"]
    if not src["has_audio"]:
        raise RuntimeError("El video no tiene pista de audio: no hay nada que transcribir.")
    prog("Extrayendo audio…", 0.05)
    audio = media.extract_audio(abs_path(pid, src["file"]), store.path(pid, "work", "source_audio.mp3"))
    prog("Transcribiendo (marcas por palabra; la primera vez local descarga el modelo ~250 MB)…", 0.1)
    tr = stt.transcribe(audio, p["settings"])
    if not tr["words"]:
        raise RuntimeError("No se detecto voz en el video.")
    with store.edit(pid) as q:
        q["analysis"]["transcript"] = tr
        q["analysis"]["points"]["transcription"] = True


def _step_translation(pid, prog):
    p = store.get(pid)
    tr = p["analysis"]["transcript"]
    segs = make_segments(tr["words"])
    prog("Traduciendo al español con Claude…", 0.3)
    system = SYSTEM_BASE + "\nTraduces del ingles al español neutro latinoamericano, natural y fiel (mismo tono de venta)."
    for a in range(0, len(segs), 60):
        chunk = segs[a:a + 60]
        payload = "\n".join(f"{a + i}\t{s['en']}" for i, s in enumerate(chunk))
        data = claude.ask_json(
            "Traduce cada linea (formato `indice<TAB>texto`). Devuelve "
            '{"translations":[{"i":<indice>,"es":"<traduccion>"}]} con TODAS las lineas.\n\n' + payload,
            system=system, model=_model(p), max_tokens=8000)
        for t in data["translations"]:
            i = int(t["i"])
            if 0 <= i < len(segs):
                segs[i]["es"] = t["es"]
    if any("es" not in s for s in segs):
        raise RuntimeError("La traduccion quedo incompleta; vuelve a lanzar la fase.")
    with store.edit(pid) as q:
        q["analysis"]["translation"] = {"segments": segs, "text": " ".join(s["es"] for s in segs)}
        q["analysis"]["points"]["translation"] = True


VISION_PROMPT = """Te muestro {n} escena(s) de un video, con 3 frames cada una (inicio, medio, final).
Describe CADA escena con precision fotografica para poder recrearla con una IA de imagen colocando a OTRA persona.
Devuelve JSON: {{"scenes":[{{
 "n": <numero de escena tal cual>,
 "shot": "tipo de plano y angulo de camara (ej. medium close-up, eye level, handheld selfie style), encuadre vertical 9:16",
 "person": "posicion exacta del cuerpo, hacia donde mira, expresion facial, gestos de manos, que sostiene o senala",
 "setting": "lugar y decorado con detalle (cocina, muebles, colores, bandera de EE.UU. si aparece y donde exactamente)",
 "props": ["objetos relevantes: frascos, plantas, ingredientes, libro, texto en pantalla..."],
 "on_screen_text": "texto visible o vacio",
 "lighting": "iluminacion y paleta de color",
 "motion": "que accion ocurre entre el frame inicial y el final",
 "summary_es": "resumen en español de 1-2 frases"
}}]}}
Se literal: solo lo que realmente se ve. Descripciones en ingles salvo summary_es."""


def _step_scenes(pid, prog):
    p = store.get(pid)
    st, src = p["settings"], p["source"]
    video = abs_path(pid, src["file"])
    prog("Detectando escenas…", 0.05)
    cuts = media.detect_scenes(video, src["duration"], threshold=float(st["scene_threshold"]),
                               max_len=float(st["max_scene_len"]), max_scenes=int(st["max_scenes"]))
    fdir = store.path(pid, "frames", ".keep").parent
    for f in fdir.glob("*.jpg"):
        f.unlink()
    scenes = []
    for i, c in enumerate(cuts):
        prog(f"Extrayendo frames de la escena {i + 1}/{len(cuts)}…", 0.05 + 0.25 * i / len(cuts))
        a, b = c["start"], c["end"]
        ts = [a + (b - a) * 0.08, (a + b) / 2, b - (b - a) * 0.08]
        files = []
        for k, t in enumerate(ts):
            files.append(store.rel(pid, media.extract_frame(video, t, fdir / f"s{i:02d}_{k}.jpg")))
        scenes.append({"idx": i, "start": a, "end": b, "frames": files, "frame": files[1]})
    words = p["analysis"]["transcript"]["words"] if p["analysis"]["transcript"] else []
    for s, ws in zip(scenes, assign_words(words, scenes)):
        s["dialogue_en"] = " ".join(w["text"] for w in ws)

    # lectura visual de cada escena (Claude vision)
    system = SYSTEM_BASE
    B = 4
    for a in range(0, len(scenes), B):
        batch = scenes[a:a + B]
        prog(f"Leyendo frames con Claude ({a + 1}-{a + len(batch)} de {len(scenes)})…", 0.3 + 0.6 * a / len(scenes))
        content = []
        for s in batch:
            content.append({"type": "text", "text": f"--- ESCENA {s['idx'] + 1} ({s['start']:.1f}s-{s['end']:.1f}s) "
                            f"| dialogo: \"{s['dialogue_en'][:300]}\" | frames inicio/medio/final:"})
            for f in s["frames"]:
                content.append(claude.image_block(abs_path(pid, f), max_side=640))
        content.append({"type": "text", "text": VISION_PROMPT.format(n=len(batch))})
        data = claude.ask_json(content, system=system, model=_model(p), max_tokens=6000)
        by_n = {int(x["n"]): x for x in data["scenes"]}
        for s in batch:
            x = by_n.get(s["idx"] + 1)
            if not x:
                raise RuntimeError(f"Claude no describio la escena {s['idx'] + 1}; reintenta la fase.")
            s["read"] = {k: x.get(k) for k in ("shot", "person", "setting", "props", "on_screen_text",
                                               "lighting", "motion", "summary_es")}
    with store.edit(pid) as q:
        q["scenes"] = scenes
        q["analysis"]["points"]["scenes"] = True
        q["analysis"]["points"]["prompts"] = False
        q["final"] = None


PROMPTS_SYS = SYSTEM_BASE + """
Escribes prompts para Nano Banana (modelo de imagen de Google) que recrean una escena de un video usando una persona
de referencia (el avatar). El avatar se entrega como imagen de referencia aparte, asi que NO describas su cara: di 'the
person from the reference photo'. Describe con precision: tipo de plano y angulo, pose corporal, direccion de la mirada,
expresion, posicion de manos y objetos, decorado con todos los detalles (cocina estadounidense, bandera, plantas, frascos...),
iluminacion. Estilo: fotografia 100% realista, natural, tipo contenido de iPhone/UGC, piel con textura real, sin aspecto de
render ni de IA. Formato vertical 9:16. Maximo 140 palabras por prompt, en ingles."""


def _step_prompts(pid, prog):
    p = store.get(pid)
    scenes = p["scenes"]
    avatar = (p["avatar"] or {}).get("profile") or {}
    notes = p["settings"].get("global_notes") or ""
    B = 8
    for a in range(0, len(scenes), B):
        batch = scenes[a:a + B]
        prog(f"Redactando prompts de imagen ({a + 1}-{a + len(batch)} de {len(scenes)})…", 0.1 + 0.85 * a / len(scenes))
        items = [{"n": s["idx"] + 1, "read": s["read"], "dialogue_en": s["dialogue_en"]} for s in batch]
        data = claude.ask_json(
            f"Avatar: {avatar.get('description', '(sin descripcion)')}\nNotas globales del usuario: {notes or '-'}\n\n"
            "Para cada escena devuelve "
            '{"scenes":[{"n":<n>,"image_prompt":"...","dialogue_es":"traduccion al español del dialogue_en de esa escena (vacio si no hay dialogo)"}]}\n\n'
            + "ESCENAS:\n" + __import__("json").dumps(items, ensure_ascii=False),
            system=PROMPTS_SYS, model=_model(p), max_tokens=6000)
        by_n = {int(x["n"]): x for x in data["scenes"]}
        with store.edit(pid) as q:
            for s in batch:
                x = by_n.get(s["idx"] + 1)
                if not x:
                    raise RuntimeError(f"Falto el prompt de la escena {s['idx'] + 1}; reintenta.")
                q["scenes"][s["idx"]]["image_prompt"] = x["image_prompt"]
                q["scenes"][s["idx"]]["dialogue_es"] = x.get("dialogue_es", "")
    with store.edit(pid) as q:
        q["analysis"]["points"]["prompts"] = True


STEPS = [("transcription", _step_transcription), ("translation", _step_translation),
         ("scenes", _step_scenes), ("prompts", _step_prompts)]


def run(pid: str, prog, force: set[str] | None = None) -> None:
    p = store.get(pid)
    if not p["source"]:
        raise RuntimeError("Primero sube el video original.")
    if not p["avatar"] or not (p["avatar"] or {}).get("profile"):
        raise RuntimeError("Primero sube el avatar (Fase 1) y espera a que termine su analisis.")
    force = set(force or [])
    # si se rehace un paso, se rehacen los dependientes
    order = [n for n, _ in STEPS]
    for n in list(force):
        force.update(order[order.index(n):])
    if "transcription" in force:
        force.add("translation")
    for i, (name, fn) in enumerate(STEPS):
        done = store.get(pid)["analysis"]["points"][name]
        if done and name not in force:
            continue
        if name in ("scenes", "prompts") and not store.get(pid)["analysis"]["points"]["transcription"]:
            raise RuntimeError("Falta la transcripcion.")
        def sub(msg=None, pr=None, _i=i):
            prog(msg, None if pr is None else (_i + pr) / 4)
        sub(f"Paso {i + 1}/4: {name}", 0)
        fn(pid, sub)

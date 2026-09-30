"""FASE 3: imagenes 100% realistas del avatar en cada escena (Nano Banana) con regeneracion por prompt."""
from __future__ import annotations

import io
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageFilter

from .. import store
from ..config import get_key
from ..services import claude, dubvoice, gemini, kie
from .common import abs_path

RULES = ("Photorealistic vertical 9:16 photograph, shot like authentic smartphone/UGC content, real skin texture, natural "
         "light, no CGI/illustration look. No captions, subtitles, watermarks or logos unless they exist in the original frame. "
         "Do not add people that are not in the original frame.")


def _layout_ref(pid: str, s: dict) -> Path:
    """Version MUY difuminada del frame original: conserva encuadre, colores y donde esta la persona,
    pero no deja ver cara ni ropa (asi la IA no puede copiar a la persona original)."""
    im = Image.open(abs_path(pid, s["frame"])).convert("RGB")
    w, h = im.size
    small = im.resize((max(24, w // 14), max(40, h // 14)), Image.BILINEAR)
    out = store.path(pid, "work", "layout", f"s{s['idx']:02d}.jpg")
    small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(5)).save(out, "JPEG", quality=85)
    return out


def _scene_text(s: dict) -> str:
    r = s.get("read") or {}
    parts = [("SHOT", r.get("shot")), ("POSE / GESTURE / GAZE / EXPRESSION (only pose, not looks)", r.get("person")),
             ("SETTING", r.get("setting")), ("PROPS", ", ".join(r.get("props") or []) if isinstance(r.get("props"), list) else r.get("props")),
             ("LIGHTING", r.get("lighting"))]
    return "\n".join(f"{k}: {v}" for k, v in parts if v)


def _action(s: dict) -> str:
    r = s.get("read") or {}
    return (s.get("action") or r.get("person") or "").strip()


def _prev_image(p: dict, idx: int):
    """Imagen generada del clip anterior (la mas cercana hacia atras que exista)."""
    for j in range(idx - 1, -1, -1):
        if (p["scenes"][j].get("image") or {}).get("file"):
            return p["scenes"][j]
    return None


def _compose_guide(p: dict, s: dict, notes: str):
    """Metodo de la guia: clip 1 = captura + avatar; siguientes = Imagen A (captura, accion) + Imagen B (imagen anterior generada)."""
    pid = p["id"]
    prev = _prev_image(p, s["idx"])
    if p["settings"].get("chain_mode") == "anchor" and s["idx"] > 0 and (p["scenes"][0].get("image") or {}).get("file"):
        prev = p["scenes"][0]        # modo rapido: todas se apoyan en el start frame (se pueden generar en paralelo)
    frame, avatar = abs_path(pid, s["frame"]), abs_path(pid, p["avatar"]["file"])
    tail = ""
    if notes:
        tail += f"\nDirector's changes (apply them): {notes}"
    if p["settings"].get("global_notes"):
        tail += f"\nGlobal style notes: {p['settings']['global_notes']}"
    if prev is None:       # primer clip: captura + avatar
        refs = [("Reference image - screenshot of the scene to recreate (use it for framing, composition, background, props and the "
                 "action/pose; its person is NOT the character):", frame),
                ("Character image - the avatar (appearance, face and clothing exactly as shown):", avatar)]
        prompt = s.get("image_prompt", "") + tail
    else:
        refs = [("Image A - screenshot of this clip (the specific action, pose and props to recreate; its person is NOT the character):", frame),
                ("Image B - generated image of the previous clip (character appearance, clothing and environment continuity):",
                 abs_path(pid, prev["image"]["file"])),
                ("Image C - the original avatar photo (the face and clothing must also match it):", avatar)]
        prompt = s.get("image_prompt", "") + " Image C is the original avatar photo: the face and clothing must also match it." + tail
    return prompt, refs


def _compose_swap(p: dict, s: dict, notes: str, anchor_idx: int | None, use_anchor: bool):
    """Edicion del frame original: se conserva la pose/manos/objetos EXACTOS y se reemplaza a la persona por el avatar."""
    pid = p["id"]
    prof = (p["avatar"] or {}).get("profile") or {}
    refs = [("IMAGE 1 - ORIGINAL SCENE to edit (keep its exact composition, body pose, hands and fingers, gaze, expression, props, "
             "graphics, background and lighting):", abs_path(pid, s["frame"])),
            ("IMAGE 2 - THE AVATAR (identity and outfit reference):", abs_path(pid, p["avatar"]["file"]))]
    anchor = None
    if anchor_idx is not None and p["scenes"][anchor_idx].get("image"):
        anchor = p["scenes"][anchor_idx]
    elif use_anchor:
        anchor = next((o for o in p["scenes"] if o["idx"] != s["idx"] and (o.get("image") or {}).get("approved")), None)
    extra = ""
    if anchor:
        refs.append(("IMAGE 3 - an approved frame of the SAME avatar (identity and outfit consistency only; ignore its pose):",
                     abs_path(pid, anchor["image"]["file"])))
        extra = " IMAGE 3 shows the avatar's approved look: match it."
    prompt = (
        "EDIT IMAGE 1: replace the person in IMAGE 1 with the person from IMAGE 2 (the avatar).\n"
        f"KEEP EXACTLY as in IMAGE 1 (mandatory): the action - {_action(s)} - the position of BOTH hands and every finger, arms, "
        "posture, gaze direction, facial expression, camera framing, table/props/graphics and their positions, background and lighting. "
        "The avatar must copy the gesture of the original person's hands. Do NOT use the pose of the avatar photo (for example crossed "
        "arms or hands clasped); use the pose of IMAGE 1.\n"
        "REPLACE COMPLETELY with the avatar from IMAGE 2: face, hairstyle, hair color, skin tone, age, facial hair, glasses, hat, body build "
        f"and ALL clothing and accessories.{extra} The original person (their face, hair, glasses and every garment, including their shirt) "
        "must not remain in any form.\n"
        f"AVATAR (must match): {prof.get('description', '')}\n"
        f"SCENE NOTES: {s.get('image_prompt', '')}\n"
        f"GLOBAL STYLE NOTES: {p['settings'].get('global_notes') or '-'}\n"
        "Remove any burned-in subtitles, captions or text overlays so the photo is clean.\n"
        + (f"EXTRA CHANGES REQUESTED (apply them): {notes}\n" if notes else "") + RULES)
    return prompt, refs


def _compose_new(p: dict, s: dict, notes: str, anchor_idx: int | None = None,
                 use_anchor: bool = True) -> tuple[str, list[tuple[str, object]]]:
    pid = p["id"]
    prof = (p["avatar"] or {}).get("profile") or {}
    mode = p["settings"].get("scene_ref_mode", "guide")
    if mode == "guide":
        return _compose_guide(p, s, notes)
    if mode == "swap":
        return _compose_swap(p, s, notes, anchor_idx, use_anchor)
    refs = [("IMAGE 1 - THE AVATAR. This is the ONLY person allowed in the result: same face, hair, skin, age, body AND the same clothes/accessories:",
             abs_path(pid, p["avatar"]["file"]))]
    layout_line = ""
    if mode == "full":
        refs.append(("IMAGE 2 - LAYOUT REFERENCE ONLY (camera framing, body pose, gesture, gaze, background, props, lighting). "
                     "The person shown here is a DIFFERENT person and must NOT appear:", abs_path(pid, s["frame"])))
        layout_line = "Use IMAGE 2 for framing, pose, background, props and lighting."
    elif mode == "blur":
        refs.append(("IMAGE 2 - a VERY BLURRED layout guide. It only shows the framing, where the person stands, and the room colors and "
                     "lighting. Ignore anything about the person in it; the person is the avatar from IMAGE 1:", _layout_ref(pid, s)))
        layout_line = "Use IMAGE 2 only as a rough guide for framing, position and room colors; recreate the details from the SCENE DESCRIPTION."
    anchor = None
    if anchor_idx is not None and p["scenes"][anchor_idx].get("image"):
        anchor = p["scenes"][anchor_idx]
    elif use_anchor:
        anchor = next((o for o in p["scenes"] if o["idx"] != s["idx"] and (o.get("image") or {}).get("approved")), None)
    extra = ""
    if anchor:
        refs.append(("IMAGE 3 - an approved frame of the SAME avatar (keep identity and outfit consistent; ignore its pose and background):",
                     abs_path(pid, anchor["image"]["file"])))
        extra = " IMAGE 3 shows the avatar's approved look: match it."
    prompt = (
        "PRIMARY GOAL: the person in IMAGE 1 (the avatar) performs THIS ACTION exactly, as clearly visible as in the original scene: "
        f"{_action(s)}\n"
        "Create ONE new photo of that avatar doing it, in the scene described below. The action, hand positions, gaze and interaction "
        "with props/graphics are mandatory and must be unmistakable.\n"
        "IDENTITY RULES (highest priority): the face, hairstyle, hair color, skin tone, facial hair, age, body build and the OUTFIT "
        "(clothes, colors, accessories, glasses) must come from IMAGE 1 only. The scene may originally have had another person "
        "(older/younger, different clothes): that person must NOT be reproduced in any way. Dress the avatar in its own outfit." + extra + "\n"
        f"AVATAR DESCRIPTION (must match): {prof.get('description', '')}\n"
        f"{layout_line}\n"
        f"SCENE (camera, pose, setting):\n{_scene_text(s)}\n"
        f"SCENE DESCRIPTION: {s.get('image_prompt', '')}\n"
        f"GLOBAL STYLE NOTES: {p['settings'].get('global_notes') or '-'}\n"
        + (f"EXTRA CHANGES REQUESTED BY THE DIRECTOR (apply them): {notes}\n" if notes else "") + RULES)
    return prompt, refs


def _compose_edit(p: dict, s: dict, notes: str) -> tuple[str, list[tuple[str, object]]]:
    pid = p["id"]
    refs = [("IMAGE 1 - AVATAR (identity reference):", abs_path(pid, p["avatar"]["file"])),
            ("IMAGE 2 - CURRENT IMAGE to edit:", abs_path(pid, s["image"]["file"]))]
    prompt = (f"Edit IMAGE 2 applying ONLY this change: {notes}\nKeep everything else identical (composition, framing, pose, "
              "setting, lighting) and keep the person's identity identical to IMAGE 1. " + RULES)
    return prompt, refs


def _call_one(provider: str, st: dict, prompt: str, refs) -> bytes:
    if provider == "dubvoice":
        labelled = prompt + "\nReferences in order: " + " | ".join(l for l, _ in refs)
        return dubvoice.image(labelled, [r for _, r in refs], model=st["dubvoice_image_model"])
    if provider == "kie":
        labelled = prompt + "\nReferences in order: " + " | ".join(l for l, _ in refs)
        return kie.nano_banana_edit(labelled, [r for _, r in refs], model=st["kie_image_model"])
    return gemini.generate_image(prompt, refs, st["image_model"])


def _call(p: dict, prompt: str, refs) -> tuple[bytes, str, list[str]]:
    """Proveedor elegido con 3 intentos; si falla, respaldo automatico en otro proveedor con key disponible."""
    st = p["settings"]
    main = st["image_provider"]
    order = [main] + [x for x in ("google", "kie") if x != main and st.get("image_fallback", True) and get_key(x)]
    last: Exception | None = None
    errs: list[str] = []
    for prov in order:
        for attempt in range(2 if prov == main else 1):
            try:
                return _call_one(prov, st, prompt, refs), prov, errs
            except Exception as e:  # noqa: BLE001
                last = e
                errs.append(f"{prov}: {str(e)[:220]}")
                if "creditos insuficientes" in str(e).lower() or "payment" in str(e).lower():
                    break
                time.sleep(4 * (attempt + 1))
    raise RuntimeError(" | ".join(errs[-3:]) or str(last))


QA_PROMPT = """Compara 3 imagenes: (1) AVATAR de referencia, (2) ESCENA ORIGINAL, (3) IMAGEN GENERADA.
La imagen generada debe mostrar a la MISMA persona del avatar (cara, pelo, edad, ropa y accesorios del avatar) haciendo la MISMA accion y
pose que la escena original (manos, dedos, mirada, objetos/graficos). Devuelve JSON:
{"same_person_as_avatar": bool, "same_outfit_as_avatar": bool, "is_copy_of_original_person": bool (true si se parece a la persona/ropa de la escena original en vez de al avatar),
 "action_matches_original": bool, "differences": "que difiere en manos/pose/mirada/objetos, en español",
 "fix": "instruccion correctiva concreta en INGLES para regenerar (que pose exacta de manos/mirada, que ropa del avatar, que objeto), maximo 2 frases"}"""


def _qa(p: dict, s: dict, data: bytes) -> dict | None:
    """Revision automatica con Claude (vision). Devuelve {ok, fix, differences} o None si no se pudo revisar."""
    try:
        pid = p["id"]
        tmp = store.path(pid, "work", "qa", f"s{s['idx']:02d}.jpg")
        Image.open(io.BytesIO(data)).convert("RGB").save(tmp, "JPEG", quality=88)
        content = [{"type": "text", "text": "(1) AVATAR:"}, claude.image_block(abs_path(pid, p["avatar"]["file"]), 640),
                   {"type": "text", "text": f"(2) ESCENA ORIGINAL (accion esperada: {_action(s)}):"},
                   claude.image_block(abs_path(pid, s["frame"]), 640),
                   {"type": "text", "text": "(3) IMAGEN GENERADA:"}, claude.image_block(tmp, 640),
                   {"type": "text", "text": QA_PROMPT}]
        r = claude.ask_json(content, model=p["settings"]["claude_model"], max_tokens=800)
        ok = bool(r.get("same_person_as_avatar") and r.get("same_outfit_as_avatar") and r.get("action_matches_original")
                  and not r.get("is_copy_of_original_person"))
        return {"ok": ok, "fix": r.get("fix", ""), "differences": r.get("differences", "")}
    except Exception:  # noqa: BLE001  - la revision es un extra: si falla, se conserva la imagen
        return None


def generate(pid: str, idx: int, mode: str = "new", notes: str = "", anchor_idx: int | None = None,
             use_anchor: bool = True) -> None:
    """mode: new (desde la escena original) | edit (retoque sobre la imagen actual)."""
    p = store.get(pid)
    s = p["scenes"][idx]
    if not p["avatar"]:
        raise RuntimeError("Falta el avatar.")
    if not s.get("image_prompt"):
        raise RuntimeError("La escena no tiene prompt; completa la Fase 2.")
    if mode == "edit" and not s.get("image"):
        mode = "new"
    with store.edit(pid) as q:
        q["scenes"][idx].update(img_state="running", img_error=None, img_started=time.time())
    st = p["settings"]
    tries = 3 if (st.get("image_qa", True) and mode == "new") else 1
    fix, qa, data, used, errs = "", None, b"", "", []
    try:
        for t in range(tries):
            n_notes = (notes + " " + fix).strip()
            if mode == "edit":
                prompt, refs = _compose_edit(p, s, n_notes)
            else:
                prompt, refs = _compose_new(p, s, n_notes, anchor_idx, use_anchor)
            data, used, errs = _call(p, prompt, refs)
            if tries == 1:
                break
            qa = _qa(p, s, data)
            if qa is None or qa.get("ok"):
                break
            fix = "CORRECTION FROM THE PREVIOUS ATTEMPT (mandatory): " + (qa.get("fix") or "")
    except Exception as e:  # noqa: BLE001
        with store.edit(pid) as q:
            q["scenes"][idx].update(img_state="error", img_error=str(e)[:500])
        raise
    im = Image.open(io.BytesIO(data)).convert("RGB")
    n = len(s.get("versions", [])) + 1
    out = store.path(pid, "images", f"s{idx:02d}_v{n}.jpg")
    im.save(out, "JPEG", quality=94)
    with store.edit(pid) as q:
        sc = q["scenes"][idx]
        v = {"file": store.rel(pid, out), "mode": mode, "notes": notes, "created": time.time(),
             "w": im.width, "h": im.height, "provider": used, "errors": errs, "qa": qa}
        sc.update(img_state=None, img_error=None)
        sc.setdefault("versions", []).append(v)
        sc["image"] = {**v, "approved": False}
        sc.pop("image_url", None)
        for c in sc.get("clips", []):        # las imagenes cambiaron: los clips deben rehacerse
            c["stale"] = True


def _generate_chain(pid: str, prog, indices: list[int]) -> None:
    """Metodo de la guia: en orden, cada imagen usa la anterior como referencia de personaje y ambiente.
    La primera (start frame) se genera sola: hay que revisarla/aprobarla antes de seguir con las demas."""
    p = store.get(pid)
    first_needs_review = indices[0] == 0 and not (p["scenes"][0].get("image") or {}).get("approved")
    todo = [0] if first_needs_review else indices
    if p["settings"].get("chain_mode") == "anchor" and not first_needs_review and len(todo) > 1:
        errors: list[str] = []
        done = [0]

        def one(i):
            try:
                generate(pid, i)
            except Exception as e:  # noqa: BLE001
                errors.append(f"Escena {i + 1}: {e}")
            done[0] += 1
            prog(f"Imagenes {done[0]}/{len(todo)} (modo rapido, en paralelo)", done[0] / len(todo))

        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda i: (time.sleep(1.0), one(i)), todo))
        if errors:
            raise RuntimeError(" | ".join(errors)[:900])
        return
    for n, i in enumerate(todo):
        prog(f"Escena {i + 1}: generando ({n + 1}/{len(todo)}) — cada imagen usa la anterior como referencia…", n / len(todo))
        try:
            generate(pid, i)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Escena {i + 1}: {e} — las siguientes dependen de esta. Corrige y pulsa 'Generar imagenes faltantes'.")
    if first_needs_review and len(indices) > 1:
        prog("ℹ️ Start frame listo. Revisa la imagen de la escena 1 (retócala si hace falta) y APRUÉBALA; después pulsa "
             "'Generar imágenes faltantes' para crear las demás.", 1.0)


def generate_many(pid: str, prog, indices: list[int]) -> None:
    """Genera las imagenes con consistencia: si no hay imagenes aprobadas, la primera se genera sola y las demas la usan
    como referencia de apariencia. Cada imagen tiene reintentos, tiempo limite y respaldo; un fallo no frena a las demas."""
    total = len(indices)
    done = [0]
    errors: list[str] = []
    p = store.get(pid)
    if p["settings"].get("scene_ref_mode", "guide") == "guide":
        return _generate_chain(pid, prog, sorted(indices))
    has_approved = any((s.get("image") or {}).get("approved") for s in p["scenes"] if s["idx"] not in indices)
    anchor = [None]

    def one(i: int):
        try:
            generate(pid, i, anchor_idx=anchor[0], use_anchor=has_approved)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Escena {i + 1}: {e}")
        done[0] += 1
        prog(f"Imagenes {done[0]}/{total} listas" + (f" ({len(errors)} con error)" if errors else ""), done[0] / total)

    rest = list(indices)
    if not has_approved and len(rest) > 1:
        first = rest.pop(0)
        prog("Generando la primera imagen (referencia de consistencia)…", 0.02)
        one(first)
        if not errors:
            anchor[0] = first
    workers = 2 if p["settings"]["image_provider"] == "dubvoice" else 3   # DubVoice: max 3 en vuelo
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda i: (time.sleep(1.5), one(i)), rest))
    if errors:
        raise RuntimeError(" | ".join(errors)[:900] + " — pulsa 'Generar imagenes faltantes' para reintentar solo las que fallaron.")


def set_approved(pid: str, idx: int, approved: bool) -> None:
    with store.edit(pid) as q:
        s = q["scenes"][idx]
        if not s.get("image"):
            raise RuntimeError("Esa escena aun no tiene imagen.")
        s["image"]["approved"] = approved


def use_version(pid: str, idx: int, n: int) -> None:
    with store.edit(pid) as q:
        s = q["scenes"][idx]
        vs = s.get("versions", [])
        if not 0 <= n < len(vs):
            raise RuntimeError("Version inexistente.")
        s["image"] = {**vs[n], "approved": False}
        s.pop("image_url", None)
        for c in s.get("clips", []):
            c["stale"] = True

"""FASE 3: imagenes 100% realistas del avatar en cada escena (Nano Banana) con regeneracion por prompt."""
from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from .. import store
from ..config import get_key
from ..services import dubvoice, gemini, kie
from .common import abs_path

RULES = ("Photorealistic vertical 9:16 photograph, shot like authentic smartphone/UGC content, real skin texture, natural "
         "light, no CGI/illustration look. No captions, subtitles, watermarks or logos unless they exist in the original frame. "
         "Do not add people that are not in the original frame.")


def _compose_new(p: dict, s: dict, notes: str, anchor_idx: int | None = None,
                 use_anchor: bool = True) -> tuple[str, list[tuple[str, object]]]:
    pid = p["id"]
    prof = (p["avatar"] or {}).get("profile") or {}
    refs = [("IMAGE 1 - THE AVATAR. This is the ONLY person allowed in the result: same face, hair, skin, age, body AND the same clothes/accessories:",
             abs_path(pid, p["avatar"]["file"])),
            ("IMAGE 2 - LAYOUT REFERENCE ONLY (camera framing, body pose, gesture, gaze, background, props, lighting). "
             "The person shown here is a DIFFERENT person and must NOT appear:", abs_path(pid, s["frame"]))]
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
        "Create ONE new photo. Start from the person in IMAGE 1 (the avatar) and place THAT exact person in the scene of IMAGE 2.\n"
        "IDENTITY RULES (highest priority): the face, hairstyle, hair color, skin tone, facial hair, age, body build and the OUTFIT "
        "(clothes, colors, accessories) must be taken from IMAGE 1 only. NEVER copy the face, hair, clothing, jewelry, glasses or "
        "accessories of the person in IMAGE 2. If the person in IMAGE 2 wears something different from the avatar, dress the avatar "
        "in the avatar's own outfit from IMAGE 1." + extra + "\n"
        f"AVATAR DESCRIPTION (must match): {prof.get('description', '')}\n"
        "SCENE RULES: from IMAGE 2 reproduce only the camera angle and framing, the body pose, hand gestures, gaze direction, "
        "facial expression, the room/background layout, props and lighting.\n"
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


def _call(p: dict, prompt: str, refs) -> tuple[bytes, str]:
    """Proveedor elegido con 3 intentos; si falla, respaldo automatico en otro proveedor con key disponible."""
    st = p["settings"]
    main = st["image_provider"]
    order = [main] + [x for x in ("google", "kie") if x != main and st.get("image_fallback", True) and get_key(x)]
    last: Exception | None = None
    for prov in order:
        for attempt in range(2 if prov == main else 1):
            try:
                return _call_one(prov, st, prompt, refs), prov
            except Exception as e:  # noqa: BLE001
                last = e
                if "creditos insuficientes" in str(e).lower() or "payment" in str(e).lower():
                    break
                time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"{last}")


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
    try:
        if mode == "edit":
            prompt, refs = _compose_edit(p, s, notes)
        else:
            prompt, refs = _compose_new(p, s, notes, anchor_idx, use_anchor)
        data, used = _call(p, prompt, refs)
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
             "w": im.width, "h": im.height, "provider": used}
        sc.update(img_state=None, img_error=None)
        sc.setdefault("versions", []).append(v)
        sc["image"] = {**v, "approved": False}
        sc.pop("image_url", None)
        for c in sc.get("clips", []):        # las imagenes cambiaron: los clips deben rehacerse
            c["stale"] = True


def generate_many(pid: str, prog, indices: list[int]) -> None:
    """Genera las imagenes con consistencia: si no hay imagenes aprobadas, la primera se genera sola y las demas la usan
    como referencia de apariencia. Cada imagen tiene reintentos, tiempo limite y respaldo; un fallo no frena a las demas."""
    total = len(indices)
    done = [0]
    errors: list[str] = []
    p = store.get(pid)
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

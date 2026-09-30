"""FASE 3: imagenes 100% realistas del avatar en cada escena (Nano Banana) con regeneracion por prompt."""
from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from .. import store
from ..services import dubvoice, gemini, kie
from .common import abs_path

RULES = ("Photorealistic vertical 9:16 photograph, shot like authentic smartphone/UGC content, real skin texture, natural "
         "light, no CGI/illustration look. No captions, subtitles, watermarks or logos unless they exist in the original frame. "
         "Do not add people that are not in the original frame.")


def _compose_new(p: dict, s: dict, notes: str) -> tuple[str, list[tuple[str, object]]]:
    pid = p["id"]
    refs = [("IMAGE 1 - AVATAR (identity reference):", abs_path(pid, p["avatar"]["file"])),
            ("IMAGE 2 - ORIGINAL FRAME of the video being replicated:", abs_path(pid, s["frame"]))]
    anchor = next((o for o in p["scenes"] if o["idx"] != s["idx"] and (o.get("image") or {}).get("approved")), None)
    extra = ""
    if anchor:
        refs.append(("IMAGE 3 - already approved frame of the same character (use ONLY for clothing and appearance consistency, "
                     "ignore its pose and background):", abs_path(pid, anchor["image"]["file"])))
        extra = " IMAGE 3 is only a consistency reference for outfit/appearance."
    prompt = (
        "Generate ONE image. The person in IMAGE 1 is the only character: keep their exact face, hair, skin tone, age and body "
        "identical. Recreate IMAGE 2 as exactly as possible - same camera angle and framing, body pose, hand gestures, gaze "
        "direction, facial expression, setting layout, props, lighting and color palette - but with the avatar from IMAGE 1 in "
        f"place of the original person.{extra}\n\nSCENE DESCRIPTION: {s.get('image_prompt', '')}\n"
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


def _call(p: dict, prompt: str, refs) -> bytes:
    st = p["settings"]
    if st["image_provider"] == "dubvoice":
        labelled = prompt + "\nReferences in order: " + " | ".join(l for l, _ in refs)
        return dubvoice.image(labelled, [r for _, r in refs], model=st["dubvoice_image_model"])
    if st["image_provider"] == "kie":
        labelled = prompt + "\nReferences in order: " + " | ".join(l for l, _ in refs)
        return kie.nano_banana_edit(labelled, [r for _, r in refs], model=st["kie_image_model"])
    return gemini.generate_image(prompt, refs, st["image_model"])


def generate(pid: str, idx: int, mode: str = "new", notes: str = "") -> None:
    """mode: new (desde la escena original) | edit (retoque sobre la imagen actual)."""
    p = store.get(pid)
    s = p["scenes"][idx]
    if not p["avatar"]:
        raise RuntimeError("Falta el avatar.")
    if not s.get("image_prompt"):
        raise RuntimeError("La escena no tiene prompt; completa la Fase 2.")
    if mode == "edit" and not s.get("image"):
        mode = "new"
    prompt, refs = (_compose_edit if mode == "edit" else _compose_new)(p, s, notes)
    data = _call(p, prompt, refs)
    im = Image.open(io.BytesIO(data)).convert("RGB")
    n = len(s.get("versions", [])) + 1
    out = store.path(pid, "images", f"s{idx:02d}_v{n}.jpg")
    im.save(out, "JPEG", quality=94)
    with store.edit(pid) as q:
        sc = q["scenes"][idx]
        v = {"file": store.rel(pid, out), "mode": mode, "notes": notes, "created": time.time(),
             "w": im.width, "h": im.height}
        sc.setdefault("versions", []).append(v)
        sc["image"] = {**v, "approved": False}
        sc.pop("image_url", None)
        for c in sc.get("clips", []):        # las imagenes cambiaron: los clips deben rehacerse
            c["stale"] = True


def generate_many(pid: str, prog, indices: list[int]) -> None:
    total = len(indices)
    done = [0]
    errors: list[str] = []

    def one(i: int):
        try:
            generate(pid, i)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Escena {i + 1}: {e}")
        done[0] += 1
        prog(f"Imagenes generadas {done[0]}/{total}", done[0] / total)

    workers = 2 if store.get(pid)["settings"]["image_provider"] == "dubvoice" else 3   # DubVoice: max 3 en vuelo
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, indices))
    if errors:
        raise RuntimeError(" | ".join(errors)[:900])


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

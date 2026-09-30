"""MODO AUTOMATICO: video en ingles + foto del avatar -> Reel final editado, sin pantalla.
Encadena las 6 fases, audita cada paso (Claude revisa imagenes y clips) y entrega el MP4 con un reporte."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Callable

from . import config, ingest, media, store
from .phases import analysis, avatar, editing, fragment, images, supervisor, videos
from .services import dubvoice


def _retry(fn, times: int, log, what: str):
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"⚠️ {what} fallo ({str(e)[:220]}). " + ("Reintento…" if i < times - 1 else "Sin mas reintentos."))
            time.sleep(3)
    raise RuntimeError(f"{what}: {last}")


def _prog(log: Callable[[str], None], phase: str):
    seen = {"m": ""}

    def p(msg=None, progress=None):
        if msg and msg != seen["m"]:
            seen["m"] = msg
            log(f"[{phase}] {msg}")
    return p


def default_out_dir() -> Path:
    home = Path.home()
    return (home / "Movies" / "REMEDIOS") if (home / "Movies").exists() else (home / "REMEDIOS")


def pick_voice(pid: str, lang: str, voice_id: str | None, gender: str | None, log) -> None:
    st = store.get(pid)["settings"]
    if voice_id:
        with store.edit(pid) as q:
            q["settings"].update(voice_id=voice_id, voice_name=voice_id, unify_voice=True)
        return
    if st.get("voice_id") or not st.get("unify_voice"):
        return
    prof = (store.get(pid)["avatar"] or {}).get("profile") or {}
    g = gender or (prof.get("voice") or {}).get("gender")
    try:
        vs = dubvoice.list_voices(g, lang) or dubvoice.list_voices(g, "")
        ranked = videos.recommend(prof, vs)
        if not ranked:
            raise RuntimeError("catalogo de voces vacio")
        top = ranked[0]
        with store.edit(pid) as q:
            q["settings"].update(voice_id=top["voice_id"], voice_name=top["name"])
        log(f"🎙️ Voz elegida para todo el video: {top['name']} ({top.get('gender') or '?'}) — la misma en todos los clips")
    except Exception as e:  # noqa: BLE001
        with store.edit(pid) as q:
            q["settings"]["unify_voice"] = False
        log(f"⚠️ No pude elegir una voz de DubVoice ({str(e)[:160]}). Sigo con la voz de Veo en cada clip (puede variar entre clips).")


def contact_sheet(video: Path, out: Path) -> Path | None:
    try:
        media.run(["-i", str(video), "-vf", "fps=1/2.5,scale=270:-2,tile=6x2", "-frames:v", "1", "-q:v", "3", str(out)])
        return out if out.exists() else None
    except Exception:  # noqa: BLE001
        return None


def build_report(pid: str, elapsed: float, warnings: list[str]) -> dict:
    p = store.get(pid)
    scenes = []
    for s in p["scenes"]:
        im = s.get("image") or {}
        qa = im.get("qa")
        scenes.append({"scene": s["idx"] + 1, "image_provider": im.get("provider"),
                       "image_review": ("ok" if (qa or {}).get("ok") else (qa or {}).get("differences") or "sin revision"),
                       "clips": [{"clip": c["idx"] + 1, "dialogue": c.get("dialogue"), "verified": bool(c.get("verified")),
                                  "attempts": c.get("attempts", 1), "seconds": round(c.get("duration") or 0, 1),
                                  "audit": c.get("audit"), "warning": c.get("warning")} for c in s.get("clips", [])]})
    fin = p.get("final") or {}
    for s in scenes:
        if s["image_review"] not in ("ok",):
            warnings.append(f"Escena {s['scene']}: imagen sin confirmar por la revision ({s['image_review']})")
        for c in s["clips"]:
            if not c["verified"]:
                warnings.append(f"Escena {s['scene']} clip {c['clip']}: no auditado/aceptado")
            if c["warning"]:
                warnings.append(f"Escena {s['scene']} clip {c['clip']}: {c['warning']}")
    if fin.get("warning"):
        warnings.append(fin["warning"])
    return {"project": pid, "voice": p["settings"].get("voice_name"), "language": p["settings"].get("output_language"),
            "final_seconds": round(fin.get("duration") or 0, 1), "silence_removed_seconds": fin.get("silence_removed"),
            "script_match": fin.get("script_match"), "scenes": scenes, "warnings": warnings, "minutes": round(elapsed / 60, 1)}


def run_full(video: Path, avatar_img: Path, *, name: str | None = None, lang: str = "es", voice_id: str | None = None,
             voice_gender: str | None = None, out_dir: Path | None = None, project: str | None = None, fast: bool = True,
             notes: str = "", stt_provider: str | None = None, log: Callable[[str], None] = print) -> dict:
    t0 = time.time()
    warnings: list[str] = []
    if project:
        pid = project
        store.get(pid)
        log(f"♻️ Retomando el proyecto {pid}")
    else:
        pid = store.create(name or Path(video).stem)["id"]
        log(f"🆕 Proyecto {pid}")
    with store.edit(pid) as q:
        q["settings"].update(output_language=lang, scene_ref_mode="guide", image_qa=True,
                             chain_mode="anchor" if fast else "sequential", dubvoice_image_model="nano-banana-pro")
        if notes:
            q["settings"]["global_notes"] = notes
        if stt_provider:
            q["settings"]["stt_provider"] = stt_provider
    p = store.get(pid)

    # FASE 1
    if not p["avatar"]:
        ingest.save_avatar(pid, Path(avatar_img))
    if not (store.get(pid)["avatar"] or {}).get("profile"):
        log("🧑 FASE 1 · Analizando el avatar…")
        _retry(lambda: avatar.run(pid, _prog(log, "F1")), 2, log, "Analisis del avatar")
    # FASE 2
    if not store.get(pid)["source"]:
        info = ingest.save_video(pid, Path(video))
        log(f"🎞️ Video cargado: {info['duration']:.1f}s {info['width']}x{info['height']}")
    if not all(store.get(pid)["analysis"]["points"].values()):
        log("📝 FASE 2 · Transcripcion, traduccion, escenas y prompts…")
        _retry(lambda: analysis.run(pid, _prog(log, "F2"), set()), 2, log, "Analisis del video")
    p = store.get(pid)
    n = len(p["scenes"])
    log(f"✅ Fase 2 completa: {n} escenas")
    # FASE 3
    if not all((s.get("image") or {}).get("approved") for s in p["scenes"]):
        log("🎨 FASE 3 · Imagenes del avatar (auditadas por Claude)…")
        if not p["scenes"][0].get("image"):
            _retry(lambda: images.generate(pid, 0), 2, log, "Start frame")
        images.set_approved(pid, 0, True)
        missing = [i for i in range(1, n) if not store.get(pid)["scenes"][i].get("image")]
        if missing:
            _retry(lambda: images.generate_many(pid, _prog(log, "F3"),
                                                [i for i in range(1, n) if not store.get(pid)["scenes"][i].get("image")]),
                   2, log, "Imagenes de las escenas")
        for i in range(n):
            if store.get(pid)["scenes"][i].get("image"):
                images.set_approved(pid, i, True)
    # FASE 4
    if not all(s.get("clips") for s in store.get(pid)["scenes"]):
        log("✂️ FASE 4 · Guion por escena y prompts de video…")
        _retry(lambda: fragment.run(pid, _prog(log, "F4")), 2, log, "Fragmentacion")
    # VOZ
    pick_voice(pid, lang, voice_id, voice_gender, log)
    # FASE 5
    clips_ok = all(c.get("status") == "done" and not c.get("stale") for s in store.get(pid)["scenes"] for c in s["clips"])
    if not clips_ok:
        log("🎬 FASE 5 · Generacion de clips con el supervisor Claude…")
        res = supervisor.start(pid, _prog(log, "F5"))
        if res and res.get("state") == "COMPLETED_WITH_WARNINGS":
            rv = "; ".join(f"escena {x['scene']} clip {x['clip']} [{x['reason']}]" for x in res.get("needs_review_list", []))
            raise RuntimeError(f"Fase 5 terminó con advertencias: {len(res.get('needs_review_list', []))} clip(s) requieren revisión ({rv}). "
                               "El video final no se arma con clips faltantes: revísalos y vuelve a lanzar.")
    # FASE 6
    log("🎞️ FASE 6 · Edicion final (recorte de silencios, subtitulos, audio)…")
    _retry(lambda: editing.run(pid, _prog(log, "F6")), 2, log, "Edicion final")
    p = store.get(pid)
    fin = p["final"]
    final_path = store.pdir(pid) / fin["file"]
    out_dir = Path(out_dir) if out_dir else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or p["name"]))[:40]
    dest = out_dir / f"REMEDIOS_{safe}_{time.strftime('%Y%m%d-%H%M')}.mp4"
    shutil.copyfile(final_path, dest)
    sheet = contact_sheet(dest, store.pdir(pid) / "final" / "contact_sheet.jpg")
    report = build_report(pid, time.time() - t0, warnings)
    report.update(video=str(dest), contact_sheet=str(sheet) if sheet else None)
    (store.pdir(pid) / "final" / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    log(f"🏁 LISTO: {dest}  ({report['final_seconds']}s, {report['minutes']} min de proceso, {len(report['warnings'])} avisos)")
    return report

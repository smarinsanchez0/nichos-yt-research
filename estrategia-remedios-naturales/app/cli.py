"""Linea de comandos de ESTRATEGIA REMEDIOS NATURALES (la usa la skill /remedios).

  python -m app.cli doctor
  python -m app.cli run --video V.mp4 --avatar A.jpg [--lang es] [--name X] [--demo]
  python -m app.cli status --project ID | edit --project ID | redo-clip ... | redo-image ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

if "--demo" in sys.argv:                                   # el modo demo nunca toca tus proyectos reales
    os.environ.setdefault("ERN_DATA_DIR", tempfile.mkdtemp(prefix="ern-demo-data-"))


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_doctor(_a) -> int:
    from . import config
    st = config.status()
    ok = True
    for k, v in st["keys"].items():
        need = k in ("anthropic", "dubvoice")
        _log(f"{'✅' if v['ok'] else ('❌' if need else '➖')} {k}: {'ok (' + v['var'] + ')' if v['ok'] else 'no encontrada'}"
             + ("" if v["ok"] or not need else "  <- NECESARIA"))
        ok = ok and (v["ok"] or not need)
    _log(f"{'✅' if st['ffmpeg'] else '❌'} ffmpeg")
    ok = ok and st["ffmpeg"]
    try:
        import faster_whisper  # noqa: F401
        _log("✅ faster-whisper (transcripcion local)")
    except Exception as e:  # noqa: BLE001  - se muestra el error REAL (no solo 'no instalado')
        _log(f"❌ faster-whisper no carga: {type(e).__name__}: {e}")
        _log("   Alternativa sin instalar nada: usa ElevenLabs Scribe -> ver `python -m app.cli run --stt elevenlabs`")
        ok = False
    if not config.get_key("google") and not config.get_key("kie"):
        _log("➖ Sin key de Google/Kie: no habra respaldo si DubVoice falla al generar imagenes")
    if config.get_key("anthropic") and not config.env("ANTHROPIC_WORKSPACE_ID"):
        _log("ℹ️ Si Anthropic responde 'not scoped to a workspace', agrega ANTHROPIC_WORKSPACE_ID a ~/.zshrc")
    return 0 if ok else 1


def cmd_run(a) -> int:
    from . import autopilot, config
    if a.demo:
        from . import demo
        demo.install()
        tmp = Path(tempfile.mkdtemp(prefix="ern-demo-in-"))
        video = demo.make_source(tmp / "demo.mp4") if a.video in (None, "demo") else Path(a.video)
        avatar = demo.make_avatar(tmp / "avatar.jpg") if a.avatar in (None, "demo") else Path(a.avatar)
    else:
        if not a.video or not a.avatar:
            print("Faltan --video y --avatar", file=sys.stderr)
            return 2
        video, avatar = Path(a.video).expanduser(), Path(a.avatar).expanduser()
        for f in (video, avatar):
            if not f.exists():
                print(f"No existe: {f}", file=sys.stderr)
                return 2
        for svc in ("anthropic", "dubvoice"):
            if not config.get_key(svc):
                print(f"Falta la API key de {svc}. Ejecuta: python -m app.cli doctor", file=sys.stderr)
                return 2
    try:
        rep = autopilot.run_full(video, avatar, name=a.name, lang=a.lang, voice_id=a.voice_id, voice_gender=a.voice_gender,
                                 out_dir=Path(a.out).expanduser() if a.out else None, project=a.project,
                                 fast=not a.sequential, notes=a.notes or "", stt_provider=a.stt, log=_log)
    except Exception as e:  # noqa: BLE001
        _log(f"❌ FALLO: {e}")
        _log("RESULT_JSON: " + json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False))
        return 1
    _log("RESULT_JSON: " + json.dumps({"ok": True, **{k: rep[k] for k in ("video", "contact_sheet", "project", "final_seconds", "warnings", "minutes")}},
                                      ensure_ascii=False))
    return 0


def cmd_status(a) -> int:
    from . import store
    p = store.get(a.project)
    print(json.dumps({"name": p["name"], "points": p["analysis"]["points"], "scenes": len(p["scenes"]),
                      "images_approved": sum(1 for s in p["scenes"] if (s.get("image") or {}).get("approved")),
                      "clips": [[c.get("status") for c in s.get("clips", [])] for s in p["scenes"]],
                      "final": (p.get("final") or {}).get("file")}, ensure_ascii=False, indent=1))
    return 0


def cmd_edit(a) -> int:
    from . import autopilot, store
    from .phases import editing
    editing.run(a.project, lambda m=None, pr=None: m and _log(f"[F6] {m}"))
    fin = store.get(a.project)["final"]
    _log("LISTO: " + str(store.pdir(a.project) / fin["file"]))
    return 0


def cmd_redo_clip(a) -> int:
    from . import store
    from .phases import videos
    videos.render_clip(a.project, a.scene - 1, a.clip - 1, prog=lambda m: _log(f"[F5] {m}"))
    _log("Clip regenerado. Ejecuta `edit` para rehacer el video final.")
    return 0


def cmd_redo_image(a) -> int:
    from .phases import images
    images.generate(a.project, a.scene - 1, "edit" if a.notes else "new", a.notes or "")
    images.set_approved(a.project, a.scene - 1, True)
    _log("Imagen regenerada y aprobada. Rehaz los clips de esa escena y luego `edit`.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="remedios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    r = sub.add_parser("run")
    r.add_argument("--video"); r.add_argument("--avatar"); r.add_argument("--name"); r.add_argument("--lang", default="es", choices=["es", "en"])
    r.add_argument("--voice-id"); r.add_argument("--voice-gender", choices=["male", "female"]); r.add_argument("--out")
    r.add_argument("--project"); r.add_argument("--notes"); r.add_argument("--sequential", action="store_true",
                                                                           help="imagenes en cadena (mas lento, mas continuidad)")
    r.add_argument("--stt", choices=["local", "elevenlabs"], default=None, help="transcripcion: local (gratis) o elevenlabs")
    r.add_argument("--demo", action="store_true"); r.set_defaults(fn=cmd_run)
    s = sub.add_parser("status"); s.add_argument("--project", required=True); s.set_defaults(fn=cmd_status)
    e = sub.add_parser("edit"); e.add_argument("--project", required=True); e.set_defaults(fn=cmd_edit)
    c = sub.add_parser("redo-clip"); c.add_argument("--project", required=True); c.add_argument("--scene", type=int, required=True)
    c.add_argument("--clip", type=int, default=1); c.set_defaults(fn=cmd_redo_clip)
    i = sub.add_parser("redo-image"); i.add_argument("--project", required=True); i.add_argument("--scene", type=int, required=True)
    i.add_argument("--notes"); i.set_defaults(fn=cmd_redo_image)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Adopta un MP4 LOCAL ya descargado para RESOLVER un AMBIGUOUS_SUBMIT de la Fase 5 (sin ningun POST ni creditos de generacion).

    python tools/f5_adopt_local.py <id_proyecto> --scene 0 --clip 1 "<ruta/al/video.mp4>" [--no-voice]

* `--scene` y `--clip` son indices desde 0 (los mismos que usa la API: /videos/<i>/<j>/...).
* CIERRE LA APP antes de ejecutarlo (dos procesos escribiendo el mismo project.json se pisarian).
* El archivo se COPIA a data/projects/<id>/adopt_inbox/ y solo esa copia se procesa. No hay endpoint HTTP con rutas.
* Se exige: MP4 valido, duracion esperada del modelo, huella visual contra el start frame de ESTA escena (y las demas), RAW preservado,
  QC visual y audio normales. NO se acepta un clip solo por darle una ruta.
* Este proceso bloquea explicitamente cualquier funcion de generacion (DubVoice/Google) y la consulta de saldo.
* El cambio de voz (si esta activado en el proyecto) usa la API de voz de DubVoice (~2.000 creditos por minuto de audio: unos 270 para 8 s).
  Con --no-voice se omite y el clip queda con audio_state=NEEDS_FIX para repetirlo luego con retry_voice.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, store  # noqa: E402
from app.phases import video_jobs as vj, videos  # noqa: E402
from app.services import dubvoice, google_veo  # noqa: E402


def _forbidden(name):
    def boom(*a, **k):
        raise RuntimeError(f"BLOQUEADO: {name} (la adopcion local no genera video ni llama proveedores de generacion)")
    return boom


def block_generation() -> None:
    videos.generate_raw = _forbidden("generate_raw")
    dubvoice.veo = _forbidden("dubvoice.veo")
    google_veo.veo = _forbidden("google_veo.veo")
    dubvoice.balance = lambda **k: None
    vj._overrides["track_balance"] = False
    vj._overrides["fallback_provider"] = "none"


def report(pid: str, si: int, ci: int) -> dict:
    p = store.get(pid)
    c = p["scenes"][si]["clips"][ci]
    f5 = c.get("f5") or {}
    att = vj.attempt_of(f5) or {}
    raw = att.get("raw")
    return {
        "clip": {"scene": si, "clip": ci, "state": f5.get("state"), "visual_state": f5.get("visual_state"), "audio_state": f5.get("audio_state"),
                 "audio_issue": f5.get("audio_issue"), "review_reason": f5.get("review_reason"), "review_message": f5.get("review_message"),
                 "legacy_status": c.get("status"), "file": c.get("file")},
        "timeline": {"source_start": f5.get("source_start"), "source_end": f5.get("source_end"), "target_duration": f5.get("target_duration"),
                     "provider_duration": att.get("provider_duration")},
        "attempt": {k: att.get(k) for k in ("id", "provider", "model", "status", "paid", "credits", "job_id", "job_id_kind", "original_filename",
                                            "submit_ambiguous", "possible_duplicate", "error_type", "resolved_ambiguity", "adoption_evidence", "raw",
                                            "bad_raw", "local_file")},
        "timing": vj.attempt_timing(att) if att else None,
        "raw_exists": bool(raw and (store.pdir(pid) / raw).exists()),
        "accounting": {"paid_attempts_f5": vj.paid_attempts(f5), "credits_spent": f5.get("credits_spent"), "round": f5.get("round"),
                       "attempt_ids": [a.get("id") for a in f5.get("attempts", [])]},
        "global": vj.summarize(p).get("state"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("file")
    ap.add_argument("--scene", type=int, required=True, help="indice de escena desde 0")
    ap.add_argument("--clip", type=int, required=True, help="indice de clip desde 0")
    ap.add_argument("--no-voice", action="store_true", help="no cambiar la voz ahora (se repite luego con retry_voice)")
    a = ap.parse_args(argv)
    block_generation()
    t0 = time.time()
    before = report(a.project, a.scene, a.clip)
    print("ANTES:", json.dumps(before, ensure_ascii=False, indent=1, default=str))
    try:
        vj.adopt_local(a.project, a.scene, a.clip, file=a.file, skip_voice=a.no_voice, log=lambda m: print("  ·", m))
    except ValueError as e:
        print(f"\nRECHAZADO (no se modifico nada): {e}")
        return 2
    after = report(a.project, a.scene, a.clip)
    print("\nDESPUES:", json.dumps(after, ensure_ascii=False, indent=1, default=str))
    ok = after["clip"]["state"] == "ACCEPTED"
    print(f"\nRESULTADO: {after['clip']['state']} ({time.time() - t0:.0f} s) · paid_attempts F5 = {after['accounting']['paid_attempts_f5']} · "
          f"creditos del clip = {after['accounting']['credits_spent']} · POST de generacion: 0 (bloqueados)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CANARIO REAL de la Fase 5 sobre UN SOLO clip: captura el contrato real de POST /api/v1/video con COMO MAXIMO UN POST pagado.

    python tools/f5_canary.py <id_proyecto> --scene 0 --clip 1                                  # SIMULACRO: no hace ninguna llamada
    python tools/f5_canary.py <id_proyecto> --scene 0 --clip 1 --yes-i-accept-one-paid-post     # canario real (1 POST, ~7.500 creditos)

Garantias (por construccion, no por buena voluntad):
  * por defecto trabaja sobre una COPIA del proyecto (`<id>-canary-<fecha>`, sin videos ni logs): el proyecto real no se toca. `--in-place` lo desactiva.
  * concurrencia 1, SIN fallback, F5_AMBIGUOUS_SUBMIT_RETRIES=0, un unico intento por clip (F5_SINGLE_POST: ni siquiera tras un 429 o un
    error de conexion se encadena un segundo POST), un unico clip (nada mas se lanza), SIN F6, SIN cambio de voz (F5_SKIP_VOICE), SIN consulta de saldo
    (salvo --balance), modelo veo-3.1-fast exigido (no se cambia nada del proyecto).
  * ademas, `dubvoice.veo` queda envuelto: la segunda llamada falla aunque algun camino de codigo intentara reenviar. Google queda bloqueado.
  * timeouts: connect 10 s, lectura del POST 300 s. Un ReadTimeout tras enviar = AMBIGUOUS_SUBMIT (0 reintentos, NEEDS_REVIEW).
  * el contrato se registra sanitizado en data/projects/<id>/f5_contract.jsonl (nunca claves, cabeceras de peticion, cookies ni firmas de URL).
Cierre la app antes de ejecutarlo.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, store  # noqa: E402
from app.phases import video_jobs as vj, videos  # noqa: E402
from app.services import dubvoice, google_veo  # noqa: E402

EXCLUDE = ("videos", "final", "work", "adopt_inbox", "f5_events.jsonl", "f5_contract.jsonl", "*.part")


def clone_project(pid: str) -> str:
    new = f"{pid}-canary-{time.strftime('%Y%m%d-%H%M%S')}"
    src, dst = store.pdir(pid), store.pdir(new)
    if dst.exists():
        raise SystemExit(f"Ya existe {dst}")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDE))
    pj = dst / "project.json"
    p = json.loads(pj.read_text())
    p["id"], p["name"] = new, f"{p.get('name', '')} (canario)"
    p["jobs"], p["f5_run"] = {}, None
    pj.write_text(json.dumps(p, ensure_ascii=False, indent=1))
    return new


def plan(pid: str, si: int, ci: int, cfg) -> dict:
    p = store.get(pid)
    sc, c = p["scenes"][si], p["scenes"][si]["clips"][ci]
    f5 = vj.get_f5(pid, si, ci)
    return {"project": pid, "scene": si, "clip": ci, "source_start": f5["source_start"], "source_end": f5["source_end"],
            "target_duration": f5["target_duration"], "timeline_source": f5["timeline_source"], "provider": cfg.primary_provider,
            "model": p["settings"].get("dubvoice_video_model"), "prompt": c.get("video_prompt"),
            "start_frame": str(store.pdir(pid) / sc["image"]["file"]), "image_approved": (sc.get("image") or {}).get("approved"),
            "f5_state": f5["state"], "attempts": [(a["id"], a.get("status"), a.get("job_id")) for a in f5["attempts"]],
            "task_id": c.get("task_id"), "legacy_status": c.get("status"), "round": f5["round"],
            "remote_unknown": f5.get("remote_unknown"), "possible_duplicate": f5.get("possible_duplicate")}


def preflight(pl: dict, f5: dict) -> list:
    bad = []
    if pl["model"] != "veo-3.1-fast":
        bad.append(f"el modelo del proyecto es {pl['model']!r}; el canario exige veo-3.1-fast (no se cambia nada del proyecto)")
    if not pl["image_approved"]:
        bad.append("la imagen de la escena no esta aprobada")
    if not pl["prompt"]:
        bad.append("el clip no tiene video_prompt")
    real = [a for a in f5["attempts"] if not a.get("legacy") and a.get("round") == f5["round"]]
    if real or f5.get("remote_unknown") or f5.get("possible_duplicate") or f5["state"] in (vj.SUBMITTED, vj.PROCESSING, vj.VALIDATING, vj.NEEDS_REVIEW):
        bad.append("el clip tiene intentos/jobs remotos previos o un estado dudoso: NO es un clip limpio")
    return bad


def contract_summary(pid: str) -> dict:
    f = store.pdir(pid) / "f5_contract.jsonl"
    recs = []
    if f.exists():
        for line in f.read_text(errors="ignore").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
    submits = [r for r in recs if r.get("op") == "submit"]
    polls = [r for r in recs if r.get("op") == "poll"]
    cfile = config.DATA_DIR / "contracts" / "dubvoice.json"
    return {"submit_records": len(submits), "first_submit": ({k: submits[0].get(k) for k in ("http_status", "elapsed_seconds", "content_type", "error",
            "header_names", "json_shape", "extracted", "url")} if submits else None),
            "polls": len(polls), "poll_endpoints": sorted({r.get("url") for r in polls if r.get("url")}),
            "statuses_seen": [r.get("raw_status") for r in recs if r.get("op") == "status_transition"],
            "unrecognized": [r.get("raw_status") for r in recs if r.get("op") == "UNRECOGNIZED_STATUS"],
            "download": [{k: r.get(k) for k in ("url", "bytes", "elapsed_ms")} for r in recs if r.get("op") == "download"],
            "contract_file": json.loads(cfile.read_text()) if cfile.exists() else None}


def final_report(pid: str, si: int, ci: int) -> dict:
    p = store.get(pid)
    c = p["scenes"][si]["clips"][ci]
    f5 = c.get("f5") or {}
    att = vj.attempt_of(f5) or {}
    return {"state": f5.get("state"), "visual_state": f5.get("visual_state"), "audio_state": f5.get("audio_state"), "audio_issue": f5.get("audio_issue"),
            "review_reason": f5.get("review_reason"), "review_message": f5.get("review_message"), "target_duration": f5.get("target_duration"),
            "provider_duration": att.get("provider_duration"), "attempt": {k: att.get(k) for k in ("id", "status", "job_id", "paid", "credits", "error_type",
            "submit_ambiguous", "possible_duplicate", "raw")}, "timing": vj.attempt_timing(att) if att else None,
            "raw_exists": bool(att.get("raw") and (store.pdir(pid) / att["raw"]).exists()), "paid_attempts_f5": vj.paid_attempts(f5) if f5 else None,
            "qc": att.get("qc"), "global": vj.summarize(p).get("state")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--scene", type=int, required=True, help="indice de escena desde 0")
    ap.add_argument("--clip", type=int, required=True, help="indice de clip desde 0")
    ap.add_argument("--in-place", action="store_true", help="usar el proyecto real en vez de una copia (si el clip ya esta ACCEPTED se abre una ronda nueva)")
    ap.add_argument("--balance", action="store_true", help="leer el saldo (GET /api/v1/me) antes y despues")
    ap.add_argument("--yes-i-accept-one-paid-post", action="store_true", dest="go", help="autoriza UN POST pagado (sin esto es un simulacro sin red)")
    a = ap.parse_args(argv)

    # ---- configuracion canario (por corrida; no toca f5_config.json ni ~/.zshrc)
    vj._overrides.update(max_concurrent_video_jobs=1, fallback_provider="none", ambiguous_submit_retries=0, max_attempts_per_clip=1, single_post=True,
                         skip_voice=True, track_balance=bool(a.balance), contract_canary=True, max_project_time=1500.0, claude_qc=True,
                         submission_connect_timeout=10.0, submission_timeout=300.0, processing_timeout=720.0, max_total_generation_time=1200.0)
    cfg = vj.config()
    pid = a.project
    work_pid = pid if a.in_place else None
    # ---- pre-vuelo sobre el proyecto de origen
    pl = plan(pid, a.scene, a.clip, cfg)
    f5 = vj.get_f5(pid, a.scene, a.clip)
    print("PLAN DEL CANARIO:", json.dumps(pl, ensure_ascii=False, indent=1, default=str))
    print("CONFIG EFECTIVA:", json.dumps({"concurrency": cfg.max_concurrent_video_jobs, "fallback": cfg.fallback_provider or "none",
                                          "ambiguous_retries": cfg.ambiguous_submit_retries, "max_attempts_per_clip": cfg.max_attempts_per_clip,
                                          "single_post": cfg.single_post, "skip_voice": cfg.skip_voice, "connect_timeout": cfg.submission_connect_timeout,
                                          "post_read_timeout": cfg.submission_timeout, "processing_timeout": cfg.processing_timeout,
                                          "max_clip_time": cfg.max_total_generation_time, "balance": cfg.track_balance}, indent=1))
    problems = preflight(pl, f5) if f5["state"] != vj.ACCEPTED else preflight(pl, dict(f5, state=vj.PENDING))
    if problems:
        print("\nABORTADO (no se hizo nada):\n  - " + "\n  - ".join(problems))
        return 2
    mode = "EN EL PROYECTO REAL" if a.in_place else "sobre una COPIA del proyecto"
    if not a.go:
        print(f"\nSIMULACRO ({mode}): 0 llamadas, 0 POST, 0 creditos. Para el canario real anada --yes-i-accept-one-paid-post")
        return 0

    # ---- barandillas duras
    posts = {"n": 0}
    real_veo = dubvoice.veo

    def one_post_only(*args, **kw):
        posts["n"] += 1
        if posts["n"] > 1:
            raise RuntimeError("BLOQUEADO: segunda llamada de generacion en modo canario (1 POST maximo)")
        return real_veo(*args, **kw)

    dubvoice.veo = one_post_only
    google_veo.veo = lambda *x, **k: (_ for _ in ()).throw(RuntimeError("BLOQUEADO: Google no se usa en el canario"))
    if work_pid is None:
        work_pid = clone_project(pid)
        print(f"\nCopia del proyecto creada: {work_pid}  (los logs del canario estaran en data/projects/{work_pid}/)")
    t0 = time.time()

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    reset_needed = vj.get_f5(work_pid, a.scene, a.clip)["state"] == vj.ACCEPTED
    log(f"INICIO del canario (zona horaria local); POST maximo = 1; reset explicito del clip ACCEPTED = {reset_needed}")
    summary = vj.run_project(work_pid, [(a.scene, a.clip)], log=log, explicit=reset_needed, paid=reset_needed)
    rep = final_report(work_pid, a.scene, a.clip)
    con = contract_summary(work_pid)
    print("\nRESULTADO DEL CLIP:", json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    print("\nCONTRATO CAPTURADO:", json.dumps(con, ensure_ascii=False, indent=1, default=str))
    ok = (rep["state"] == vj.ACCEPTED and posts["n"] == 1 and rep["raw_exists"])
    print(f"\nPOST de generacion realizados por este proceso: {posts['n']} · registros 'submit' en f5_contract.jsonl: {con['submit_records']} · "
          f"intentos F5 pagados: {rep['paid_attempts_f5']} · duracion total {time.time() - t0:.0f} s · proyecto: {work_pid}")
    print("CANARY:", "PASS (ACCEPTED, RAW guardado, 1 POST)" if ok else f"STOPPED/REVISAR (estado {rep['state']}; {rep.get('review_reason')})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

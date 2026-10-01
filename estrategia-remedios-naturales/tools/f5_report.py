#!/usr/bin/env python3
"""Reporte de SOLO LECTURA de la Fase 5 de un proyecto (sin red, sin claves): una fila por intento con lo que hay que anotar en la
prueba real: hora de envio, job_id, proveedor, sondeos, tiempo hasta completar, duracion recibida vs target, resultado, reintentos,
error y creditos. Uso:

    python tools/f5_report.py <id_proyecto> [--events 25] [--clips 7.1,8.1,8.2]

`--clips` acepta escena.clip (numeracion desde 1). Los datos salen de data/projects/<id>/project.json, f5_events.jsonl y
data/contracts/dubvoice.json (todos sanitizados por la propia app).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.phases import video_jobs as vj  # noqa: E402


def _t(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"


def _s(v, nd=0):
    return "-" if v is None else f"{v:.{nd}f}s"


def build(pid: str, only: set | None = None, events: int = 25) -> str:
    base = config.PROJECTS_DIR / pid
    p = json.loads((base / "project.json").read_text())
    out = []
    run = p.get("f5_run") or {}
    out.append(f"PROYECTO {pid} · estado F5: {run.get('state', '-')} · inicio {_t(run.get('started_at'))} · fin {_t(run.get('finished_at'))}")
    summ = run.get("summary") or {}
    if summ:
        out.append(f"  aceptados {summ.get('accepted')}/{summ.get('clips_total')} · en revision {summ.get('needs_review')} · fallidos {summ.get('failed')} · "
                   f"intentos pagados {summ.get('paid_attempts')} · reintentos {summ.get('retries')} · creditos aprox {summ.get('credits_estimated')} · "
                   f"errores {summ.get('errors_by_type')}")
    out.append("")
    hdr = ("clip", "estado", "int", "prov/modelo", "enviado", "job_id", "ack", "sondeos", "t_gen", "recup", "recibido", "target", "tramo origen", "resultado", "error", "creditos")
    rows = [hdr]
    for si, s in enumerate(p.get("scenes", [])):
        for ci, c in enumerate(s.get("clips", [])):
            if only and (si + 1, ci + 1) not in only:
                continue
            f5 = c.get("f5")
            if not f5:
                rows.append((f"{si + 1}.{ci + 1}", c.get("status", "-"), "-", "-", "-", c.get("task_id") or "-", "-", "-", "-", "-", f"{c.get('duration') or '-'}",
                             f"{c.get('target') or '-'}", "-", "(sin f5: proyecto antiguo)", c.get("error") or "-", "-"))
                continue
            tl = f"{f5.get('source_start')}→{f5.get('source_end')}" if f5.get("source_start") is not None else "-"
            for a in f5.get("attempts", []) or [{}]:
                tm = vj.attempt_timing(a)
                res = a.get("status", "-")
                if f5["state"] in ("NEEDS_REVIEW", "FAILED") and a.get("id") == f5.get("current_attempt"):
                    res = f"{res}→{f5['state']}:{f5.get('review_reason')}"
                rows.append((f"{si + 1}.{ci + 1}", f5["state"] + f"/{f5.get('audio_state', '-')}", a.get("id", "-"),
                             f"{a.get('provider', '-')}/{a.get('model') or '-'}", _t(a.get("submitted_at")), a.get("job_id") or "-",
                             _s(tm["submit_ack_seconds"], 1), str(a.get("polls", "-")), _s(tm["generation_seconds"]), _s(tm["recovery_delay_seconds"]),
                             str(a.get("provider_duration", "-")),
                             str(f5.get("target_duration")), tl, res, a.get("error_type") or "-", str(a.get("credits") or "-")))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(hdr))]
    for r in rows:
        out.append("  ".join(str(x).ljust(widths[i]) for i, x in enumerate(r)))
    out.append("  t_gen = tiempo de generacion observado de principio a fin; '-' si se desconoce (job recuperado: ver recup = envio original → recuperacion, NO es tiempo de generacion)")
    cf = config.DATA_DIR / "contracts" / "dubvoice.json"
    out.append("")
    if cf.exists():
        c = json.loads(cf.read_text())
        out.append(f"CONTRATO DUBVOICE verificado={c.get('verified')} · campo del job_id={c.get('submit_id_field')} · ruta de sondeo={c.get('poll_endpoint')}"
                   f"?{c.get('poll_param')} · estados vistos={c.get('statuses_seen')} · campo de estado={c.get('status_field')} · campo de URL={c.get('result_field')}")
    else:
        out.append("CONTRATO DUBVOICE: aun NO verificado (falta un ciclo completo). Revisa f5_contract.jsonl")
    ev = base / "f5_events.jsonl"
    if ev.exists() and events:
        out.append("")
        out.append(f"ULTIMOS {events} EVENTOS (f5_events.jsonl):")
        for line in ev.read_text(errors="ignore").splitlines()[-events:]:
            try:
                e = json.loads(line)
                extra = {k: v for k, v in e.items() if k not in ("ts", "t", "kind")}
                out.append(f"  {e.get('t')} {e.get('kind'):<14} {json.dumps(extra, ensure_ascii=False)[:230]}")
            except ValueError:
                pass
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--events", type=int, default=25)
    ap.add_argument("--clips", default="", help="escena.clip separados por coma (desde 1), p. ej. 7.1,8.1,8.2")
    a = ap.parse_args()
    only = {tuple(int(x) for x in t.split(".")) for t in a.clips.split(",") if t.strip()} or None
    print(build(a.project, only, a.events))

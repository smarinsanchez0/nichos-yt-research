"""ESTRATEGIA REMEDIOS NATURALES - API + interfaz web."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import config, jobs, media, store
from .phases import analysis, avatar, editing, fragment, images, supervisor, video_jobs, videos
from .services import dubvoice, eleven

app = FastAPI(title="ESTRATEGIA REMEDIOS NATURALES")
STATIC = Path(__file__).parent / "static"
jobs.reset_stale()


def P(pid: str) -> dict:
    try:
        return store.get(pid)
    except KeyError:
        raise HTTPException(404, "Proyecto no encontrado")


def _start(pid: str, name: str, fn):
    if not jobs.start(pid, name, fn):
        raise HTTPException(409, "Esa tarea ya se esta ejecutando")
    return {"ok": True}


def _need(cond: bool, msg: str):
    if not cond:
        raise HTTPException(400, msg)


# ------------------------------------------------------------------ general
@app.get("/api/status")
def status():
    return config.status()


@app.get("/api/projects")
def projects():
    return store.list_projects()


@app.post("/api/projects")
def create_project(body: dict = Body(default={})):
    return store.create((body.get("name") or "").strip())


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return P(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    P(pid)
    store.delete(pid)
    return {"ok": True}


@app.patch("/api/projects/{pid}/settings")
def patch_settings(pid: str, body: dict = Body(...)):
    P(pid)
    allowed = set(store.DEFAULT_SETTINGS) - {"_v"}
    with store.edit(pid) as p:
        for k, v in body.items():
            if k in allowed:
                p["settings"][k] = v
        if "name" in body and body["name"]:
            p["name"] = str(body["name"])[:80]
    return {"ok": True}


@app.post("/api/projects/{pid}/jobs/{name}/reset")
def reset_job(pid: str, name: str):
    P(pid)
    if name in ("videos", "supervisor") or name.startswith("clip:"):
        video_jobs.stop_project(pid)          # F5: cancela los drivers; los jobs remotos se conservan y se reconcilian (no se re-pagan)
    jobs.force_reset(pid, name)
    return {"ok": True}


@app.get("/files/{pid}/{path:path}")
def files(pid: str, path: str):
    base = store.pdir(pid).resolve()
    f = (base / path).resolve()
    if base not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ fase 1
@app.post("/api/projects/{pid}/avatar")
async def upload_avatar(pid: str, file: UploadFile = File(...)):
    P(pid)
    raw = await file.read()
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, "El archivo no es una imagen valida")
    im.thumbnail((2048, 2048))
    out = store.path(pid, "avatar", "avatar.jpg")
    im.save(out, "JPEG", quality=95)
    with store.edit(pid) as p:
        p["avatar"] = {"file": store.rel(pid, out), "w": im.width, "h": im.height, "profile": None}
    _start(pid, "avatar", lambda prog: avatar.run(pid, prog))
    return {"ok": True}


@app.post("/api/projects/{pid}/avatar/analyze")
def reanalyze_avatar(pid: str):
    _need(bool(P(pid)["avatar"]), "Sube primero el avatar")
    return _start(pid, "avatar", lambda prog: avatar.run(pid, prog))


@app.patch("/api/projects/{pid}/avatar/profile")
def edit_profile(pid: str, body: dict = Body(...)):
    _need(bool(P(pid)["avatar"]), "Sube primero el avatar")
    with store.edit(pid) as p:
        prof = p["avatar"].get("profile") or {}
        for k in ("description", "gender", "age_range", "summary_es"):
            if k in body:
                prof[k] = body[k]
        if isinstance(body.get("voice"), dict):
            prof["voice"] = {**prof.get("voice", {}), **body["voice"]}
        p["avatar"]["profile"] = prof
    return {"ok": True}


# ------------------------------------------------------------------ fase 2
@app.post("/api/projects/{pid}/video")
async def upload_video(pid: str, file: UploadFile = File(...)):
    P(pid)
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    out = store.path(pid, "source", f"original{ext}")
    with open(out, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    info = media.probe(out)
    if not info["has_video"] or info["duration"] <= 0:
        out.unlink(missing_ok=True)
        raise HTTPException(400, "No se pudo leer el video (formato no soportado).")
    with store.edit(pid) as p:
        p["source"] = {"file": store.rel(pid, out), "name": file.filename, **info}
        p["analysis"] = {"points": {"transcription": False, "translation": False, "scenes": False, "prompts": False},
                         "transcript": None, "translation": None}
        p["scenes"], p["final"] = [], None
    return {"ok": True, **info}


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str, body: dict = Body(default={})):
    p = P(pid)
    _need(bool(p["source"]), "Sube primero el video original")
    _need(bool((p["avatar"] or {}).get("profile")), "Sube el avatar y espera su analisis (Fase 1)")
    force = set(body.get("force") or [])
    if body.get("restart"):
        force = {"transcription"}
    return _start(pid, "analysis", lambda prog: analysis.run(pid, prog, force))


@app.patch("/api/projects/{pid}/scenes/{i}")
def patch_scene(pid: str, i: int, body: dict = Body(...)):
    P(pid)
    with store.edit(pid) as p:
        _need(0 <= i < len(p["scenes"]), "Escena inexistente")
        for k in ("image_prompt", "dialogue_es", "dialogue_en", "action"):
            if k in body:
                p["scenes"][i][k] = body[k]
                if k == "action":
                    p["scenes"][i]["action_edited"] = True
    return {"ok": True}


# ------------------------------------------------------------------ fase 3
def _ready_for_images(p):
    _need(bool(p["avatar"]), "Falta el avatar")
    _need(p["analysis"]["points"]["prompts"] and bool(p["scenes"]), "Completa la Fase 2 (4 puntos) primero")


@app.post("/api/projects/{pid}/images/generate")
def gen_images(pid: str, body: dict = Body(default={})):
    p = P(pid)
    _ready_for_images(p)
    idxs = body.get("scenes")
    if idxs is None:
        idxs = [s["idx"] for s in p["scenes"] if not s.get("image")]
    idxs = [int(i) for i in idxs]
    _need(bool(idxs), "Todas las escenas ya tienen imagen. Usa regenerar en la que quieras cambiar.")
    return _start(pid, "images", lambda prog: images.generate_many(pid, prog, idxs))


@app.post("/api/projects/{pid}/images/{i}/regenerate")
def regen_image(pid: str, i: int, body: dict = Body(default={})):
    p = P(pid)
    _ready_for_images(p)
    _need(0 <= i < len(p["scenes"]), "Escena inexistente")
    mode = body.get("mode", "edit")
    notes = (body.get("notes") or "").strip()
    _need(mode == "new" or bool(notes), "Escribe el cambio que quieres en la imagen")
    return _start(pid, f"img:{i}", lambda prog: images.generate(pid, i, mode, notes))


@app.post("/api/projects/{pid}/images/{i}/approve")
def approve(pid: str, i: int, body: dict = Body(default={"approved": True})):
    P(pid)
    try:
        images.set_approved(pid, i, bool(body.get("approved", True)))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/projects/{pid}/images/{i}/version/{n}")
def pick_version(pid: str, i: int, n: int):
    P(pid)
    try:
        images.use_version(pid, i, n)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/projects/{pid}/images/approve_all")
def approve_all(pid: str):
    with store.edit(pid) as p:
        for s in p["scenes"]:
            if s.get("image"):
                s["image"]["approved"] = True
    return {"ok": True}


# ------------------------------------------------------------------ fase 4
@app.post("/api/projects/{pid}/fragment")
def do_fragment(pid: str):
    p = P(pid)
    _need(bool(p["scenes"]), "Completa la Fase 2")
    _need(all((s.get("image") or {}).get("approved") for s in p["scenes"]),
          "Aprueba todas las imagenes de la Fase 3 primero")
    return _start(pid, "fragment", lambda prog: fragment.run(pid, prog))


@app.patch("/api/projects/{pid}/scenes/{i}/clips/{j}")
def patch_clip(pid: str, i: int, j: int, body: dict = Body(...)):
    p = P(pid)
    _need(0 <= i < len(p["scenes"]) and 0 <= j < len(p["scenes"][i].get("clips", [])), "Clip inexistente")
    fragment.update_clip(pid, i, j, body)
    return {"ok": True}


# ------------------------------------------------------------------ fase 5
@app.get("/api/projects/{pid}/voices")
def get_voices(pid: str):
    p = P(pid)
    try:
        prof = (p["avatar"] or {}).get("profile") or {}
        if p["settings"].get("voice_provider") == "elevenlabs":
            vs = eleven.list_voices()
        else:
            g, lg = (prof.get("voice") or {}).get("gender"), p["settings"].get("output_language", "es")
            vs = dubvoice.list_voices(g, lg) or dubvoice.list_voices(g, "")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    return {"voices": videos.recommend((p["avatar"] or {}).get("profile"), vs)[:40],
            "selected": p["settings"].get("voice_id")}


@app.get("/api/projects/{pid}/videos/estimate")
def videos_estimate(pid: str):
    return videos.estimate(P(pid))


@app.post("/api/projects/{pid}/videos/generate")
def gen_videos(pid: str, body: dict = Body(default={})):
    p = P(pid)
    _need(bool(p["scenes"]) and all(s.get("clips") for s in p["scenes"]), "Completa la Fase 4 primero")
    if p["settings"].get("unify_voice"):
        _need(bool(p["settings"].get("voice_id")), "Elige una voz (o desactiva 'unificar voz')")
    pairs = body.get("clips")
    explicit = pairs is not None
    if pairs is None:
        pairs = [(s["idx"], c["idx"]) for s in p["scenes"] for c in s["clips"]
                 if video_jobs.needs_work(c) or c.get("status") != "done"]
    pairs = [(int(a), int(b)) for a, b in pairs]
    _need(bool(pairs), "Todos los clips estan listos")
    return _start(pid, "videos", lambda prog: videos.render_many(pid, prog, pairs, explicit))


@app.post("/api/projects/{pid}/videos/salvage")
def videos_salvage(pid: str):
    p = P(pid)
    _need(bool(p["scenes"]) and all(s.get("clips") for s in p["scenes"]), "Completa la Fase 4 primero")
    return _start(pid, "salvage", lambda prog: videos.salvage_all(pid, prog))


@app.post("/api/projects/{pid}/supervisor/start")
def supervisor_start(pid: str):
    p = P(pid)
    _need(bool(p["scenes"]) and all(s.get("clips") for s in p["scenes"]), "Completa la Fase 4 primero")
    _need(all((s.get("image") or {}).get("approved") for s in p["scenes"]), "Aprueba todas las imagenes de la Fase 3")
    if p["settings"].get("unify_voice"):
        _need(bool(p["settings"].get("voice_id")), "Elige una voz (o desactiva 'unificar voz')")
    with store.edit(pid) as q:
        q["jobs"].pop("supervisor", None)
    return _start(pid, "supervisor", lambda prog: supervisor.start(pid, prog))


@app.post("/api/projects/{pid}/supervisor/stop")
def supervisor_stop(pid: str):
    P(pid)
    supervisor.stop(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/videos/{i}/{j}/regenerate")
def regen_clip(pid: str, i: int, j: int, paid: bool = False):
    """Regenerar un clip = accion EXPLICITA. Pasa por el planificador unico de F5. Si el clip esta en revision solo por un fallo de
    post-proceso (o un job remoto conocido) primero se intenta la recuperacion GRATIS; `?paid=1` fuerza una generacion nueva."""
    p = P(pid)
    _need(0 <= i < len(p["scenes"]) and 0 <= j < len(p["scenes"][i].get("clips", [])), "Clip inexistente")

    def go(prog):
        if paid:
            video_jobs.reset_clip(pid, i, j, why="user_paid")
        video_jobs.run_project(pid, [(i, j)], prog=prog, explicit=True)
    return _start(pid, f"clip:{i}:{j}", go)


@app.get("/api/projects/{pid}/f5/summary")
def f5_summary(pid: str):
    """Telemetria de la Fase 5 (solo lectura): estado global, totales, errores por tipo, tiempos, creditos e historial por clip."""
    return video_jobs.summarize(P(pid))


@app.get("/api/projects/{pid}/f5/clip/{i}/{j}")
def f5_clip(pid: str, i: int, j: int):
    p = P(pid)
    _need(0 <= i < len(p["scenes"]) and 0 <= j < len(p["scenes"][i].get("clips", [])), "Clip inexistente")
    c = p["scenes"][i]["clips"][j]
    return video_jobs.ensure(c, p["scenes"][i], p["scenes"][i]["clips"]) if not c.get("f5") else c["f5"]


# ------------------------------------------------------------------ fase 6
@app.post("/api/projects/{pid}/edit")
def do_edit(pid: str):
    p = P(pid)
    _need(bool(p["scenes"]), "Completa las fases anteriores")
    return _start(pid, "edit", lambda prog: editing.run(pid, prog))


@app.exception_handler(Exception)
async def boom(_, exc: Exception):
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")

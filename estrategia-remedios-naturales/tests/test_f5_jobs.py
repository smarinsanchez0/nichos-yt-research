"""Fase 5 · motor de trabajos de video (maquina de estados, politica por error, slots, timeouts, reconciliacion, telemetria).

Todo es SIMULADO: ningun test llama a una API real (proveedores, Claude y Whisper se sustituyen). ffmpeg es real (genera MP4 de prueba).
Ejecuta:  pytest -q tests/test_f5_jobs.py
"""
from __future__ import annotations
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("ERN_DATA_DIR", tempfile.mkdtemp(prefix="ern-test-"))

from app import config, jobs, media, store  # noqa: E402
from app.phases import supervisor, video_jobs as vj, videos  # noqa: E402
from app.services import claude, dubvoice, errors, google_veo, http, kie, stt  # noqa: E402
from app.services.errors import ErrorType, F5Error  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="f5-media-"))
DLG = "ginger tea can calm your stomach"
_video_cache: dict = {}


def make_video(seconds: float) -> bytes:
    if seconds not in _video_cache:
        out = TMP / f"v{seconds}.mp4"
        media.run(["-f", "lavfi", "-i", f"testsrc=s=360x640:d={seconds}:r=24", "-f", "lavfi", "-i",
                   f"aevalsrc='0.5*sin(2*PI*300*t)*lt(t,{min(seconds, 4.2)})':d={seconds}:s=44100", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-shortest", str(out)])
        _video_cache[seconds] = out.read_bytes()
    return _video_cache[seconds]


def mp3(seconds: float = 4) -> bytes:
    f = TMP / "voice.mp3"
    media.run(["-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*500*t)*lt(t,{seconds})':d={seconds}:s=44100", str(f)])
    return f.read_bytes()


# ------------------------------------------------------------------ proyecto y proveedores simulados
def make_project(scenes: int = 1, clips: int = 1, dur: float = 3.0, start: float = 10.0, target: float = 4.0, unify: bool = False) -> str:
    pid = store.create("f5")["id"]
    img = store.path(pid, "images", "i.jpg")
    Image.new("RGB", (100, 180)).save(img)
    with store.edit(pid) as q:
        q["settings"].update(unify_voice=unify, voice_id="v1" if unify else None, output_language="en", dubvoice_video_model="veo-3.1-fast")
        q["scenes"] = [{"idx": i, "start": start + i * dur, "end": start + (i + 1) * dur, "image": {"file": store.rel(pid, img), "approved": True},
                        "frame": store.rel(pid, img),
                        "clips": [{"idx": k, "dialogue": DLG, "video_prompt": f"prompt s{i}c{k}", "target": target, "status": "pending", "lang": "en",
                                   "t_start": start + i * dur + 0.2, "t_end": start + (i + 1) * dur - 0.2} for k in range(clips)]}
                       for i in range(scenes)]
    return pid


class Fake:
    """Proveedor simulado. `script` = lista de pasos (callables ctx -> bytes | raise); pasado el final se usa un exito con VIDEO de 8 s."""

    def __init__(self, name="dubvoice", script=None, delay=0.0, video=None):
        self.name, self.script, self.delay, self.video = name, list(script or []), delay, video
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.resumes: list[str] = []
        self.inflight = self.peak = 0
        self.resume_script: list = []
        self.by_prompt: dict = {}

    # -- pasos de guion
    def veo(self, prompt, image_path, model="veo-3.1-fast", aspect="9:16", duration=8, progress=None, cancel=None, on_submit=None, on_poll=None,
            limits=None, gate=None, recorder=None, **kw):
        with self.lock:
            n = len(self.calls)
            self.calls.append({"prompt": prompt, "model": model, "duration": duration, "t": time.time(), "limits": limits})
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        job = f"{self.name}-job-{n}"
        ctx = types_ns(job=job, on_submit=on_submit, on_poll=on_poll, cancel=cancel, fake=self)
        try:
            step = self.by_prompt.get(prompt) or (self.script[n] if n < len(self.script) else None)
            if step is None:
                if on_submit:
                    on_submit(job)
                if self.delay:
                    time.sleep(self.delay)
                return job, self.video or make_video(8)
            return job, step(ctx)
        finally:
            with self.lock:
                self.inflight -= 1

    def resume(self, job_id, model=None, progress=None, cancel=None, on_poll=None, limits=None, gate=None, recorder=None, **kw):
        with self.lock:
            n = len(self.resumes)
            self.resumes.append(job_id)
        step = self.resume_script[n] if n < len(self.resume_script) else None
        if step is None:
            return job_id, self.video or make_video(8)
        return job_id, step(types_ns(job=job_id, on_submit=None, on_poll=on_poll, cancel=cancel, fake=self))


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def ok(video=None, delay=0.0):
    def step(ctx):
        if ctx.on_submit:
            ctx.on_submit(ctx.job)
        if delay:
            time.sleep(delay)
        return video or make_video(8)
    return step


def fail_before_submit(exc):
    def step(ctx):
        raise exc
    return step


def fail_after_submit(exc):
    def step(ctx):
        if ctx.on_submit:
            ctx.on_submit(ctx.job)
        raise exc
    return step


def hang_after_submit(release: threading.Event):
    def step(ctx):
        if ctx.on_submit:
            ctx.on_submit(ctx.job)
        release.wait(30)
        return make_video(8)
    return step


CLAUDE_CALLS: list = []


def default_claude(content, *, system="", model="", max_tokens=0):
    txt = content[-1]["text"] if isinstance(content, list) else content
    CLAUDE_CALLS.append(txt.split(":")[0].split("(")[0][:20].strip())
    if txt.startswith("QC VISUAL"):
        return {"visual_ok": True, "reasons": [], "prompt_fix": ""}
    if txt.startswith("REESCRIBE PROMPT"):
        default_claude.n += 1
        return {"prompt": f"rewritten prompt #{default_claude.n}"}
    raise AssertionError("Claude no deberia recibir esto en F5: " + txt[:100])


default_claude.n = 0


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test-key")
    monkeypatch.setenv("DUBVOICE_API_KEY", "d-test-key")
    vj.SCHED.reset_for_tests()
    vj._overrides.clear()
    vj._overrides.update(track_balance=False, reconcile_interval=0.05, reconcile_timeout=1.0, poll_interval=0.05, submission_timeout=5, poll_request_timeout=2, processing_timeout=6, download_timeout=5,
                         post_timeout=8, audit_timeout=8, claude_timeout=5, claude_qc_retries=1, contract_canary=False,
                         video_requests_per_minute=6000, max_concurrent_video_jobs=3, watchdog_margin=2)
    CLAUDE_CALLS.clear()
    for name in ("WAIT_TIMEOUT_RETRY", "WAIT_AMBIGUOUS", "WAIT_UNKNOWN", "WAIT_DOWNLOAD"):
        monkeypatch.setattr(vj, name, 0.0)
    monkeypatch.setattr(vj, "BACKOFF_RATE", (0.2, 0.3, 0.4, 0.5))
    monkeypatch.setattr(vj, "BACKOFF_CONN", (0.05, 0.05, 0.05))
    dubvoice._poll_cache.clear()
    monkeypatch.setattr(claude, "ask_json", default_claude)
    monkeypatch.setattr(stt, "transcribe", lambda audio, settings=None, language_code=None: {"text": DLG, "words": []})
    monkeypatch.setattr(kie, "upload_file", lambda path, mime="audio/mpeg", folder="": "https://example.com/a.mp3")
    yield
    for runs in list(vj.SCHED.runs.values()):                 # higiene: un test que fallo no deja corridas colgadas contaminando al siguiente
        for r in list(runs):
            r.cancel.set()
    end = time.time() + 5
    while vj.SCHED.active and time.time() < end:
        time.sleep(0.05)
    vj._overrides.clear()


def install(monkeypatch, dub: Fake | None = None, goog: Fake | None = None):
    dub = dub or Fake("dubvoice")
    goog = goog or Fake("google")
    monkeypatch.setattr(dubvoice, "veo", dub.veo)
    monkeypatch.setattr(dubvoice, "resume", dub.resume, raising=False)
    monkeypatch.setattr(google_veo, "veo", goog.veo)
    monkeypatch.setattr(google_veo, "resume", goog.resume, raising=False)
    return dub, goog


def clips_of(pid):
    return [c for s in store.get(pid)["scenes"] for c in s["clips"]]


def f5_of(pid, si=0, ci=0):
    return store.get(pid)["scenes"][si]["clips"][ci]["f5"]


def first_job_id(pid):
    a = ((clips_of(pid)[0].get("f5") or {}).get("attempts")) or [{}]
    return a[0].get("job_id")


def run(pid, **kw):
    logs = []
    res = vj.run_project(pid, log=logs.append, **kw)
    return res, logs


# ================================================================== camino feliz y estructura
def test_happy_path_two_clips_and_legacy_fields(monkeypatch):
    dub, goog = install(monkeypatch)
    pid = make_project(scenes=2)
    res, logs = run(pid)
    assert res["state"] == "COMPLETED" and res["accepted"] == 2
    for c in clips_of(pid):
        f5 = c["f5"]
        assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK"
        # campos heredados (F6 / UI) siguen actualizandose
        assert c["status"] == "done" and c["file"] and c["raw"] and c["error"] is None and c["provider_used"] == "dubvoice"
        assert c["duration"] > 1 and c["task_id"].startswith("dubvoice-job") and c["verified"] is True and c["target"] == 4.0
        assert (store.pdir(pid) / c["file"]).exists() and (store.pdir(pid) / c["raw"]).exists()
        a = f5["attempts"][0]
        assert a["status"] == "VALIDATED" and a["job_id"] == c["task_id"] and a["prompt"].startswith("prompt s") and a["paid"] and a["credits"] == 7500
    assert len(dub.calls) == 2 and len(goog.calls) == 0
    assert any("Aceptado" in x for x in logs) and any("Terminado: 2/2" in x for x in logs)
    assert (store.pdir(pid) / "f5_events.jsonl").exists()
    assert store.get(pid)["f5_run"]["state"] == "COMPLETED"


def test_job_id_is_persisted_immediately_on_submit(monkeypatch):
    release = threading.Event()
    dub, _ = install(monkeypatch, Fake(script=[hang_after_submit(release)]))
    pid = make_project()
    t = threading.Thread(target=lambda: vj.run_project(pid, log=lambda m: None), daemon=True)
    t.start()
    end = time.time() + 10
    seen = None
    while time.time() < end:
        c = clips_of(pid)[0]
        a = (c.get("f5") or {}).get("attempts") or []
        if a and a[0].get("job_id"):
            seen = (c["f5"]["state"], a[0]["job_id"], c["status"], c["task_id"])
            break
        time.sleep(0.02)
    assert seen and seen[1] == "dubvoice-job-0"                     # el ID esta en project.json MIENTRAS el video aun se genera
    assert seen[0] in ("SUBMITTED", "PROCESSING") and seen[2] == "running" and seen[3] == "dubvoice-job-0"
    release.set()
    t.join(30)
    assert f5_of(pid)["state"] == "ACCEPTED"


def test_8s_provider_clip_for_3s_target_is_visually_accepted(monkeypatch):
    """source 10→13 s, target 3.0; el proveedor devuelve 8 s: ACCEPTED, sin retry, sin 'duration mismatch', sin regenerar."""
    dub, goog = install(monkeypatch)
    pid = make_project(start=10.0, dur=3.0, target=4.0)
    with store.edit(pid) as q:
        q["scenes"][0]["clips"][0]["t_start"], q["scenes"][0]["clips"][0]["t_end"] = 10.0, 13.0
    res, _ = run(pid)
    f5 = f5_of(pid)
    assert f5["source_start"] == 10.0 and f5["source_end"] == 13.0 and f5["target_duration"] == 3.0
    a = f5["attempts"][0]
    assert a["target_duration"] == 3.0 and 7.5 <= a["provider_duration"] <= 8.5
    assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK" and len(f5["attempts"]) == 1
    assert len(dub.calls) == 1 and clips_of(pid)[0]["warning"] is None
    assert f5["rate_limit_hits"] == 0 and f5["cf_hits"] == 0


def test_timeline_is_derived_from_the_original_scene_not_from_spanish_target():
    scene = {"start": 20.0, "end": 26.0}
    a = {"idx": 0, "t_start": 20.3, "t_end": 22.0, "target": 9.9}
    b = {"idx": 1, "t_start": 23.0, "t_end": 25.8, "target": 9.9}
    tl_a = vj.compute_timeline(scene, a, [a, b])
    tl_b = vj.compute_timeline(scene, b, [a, b])
    assert (tl_a["source_start"], tl_a["source_end"]) == (20.0, 22.5) and tl_a["timeline_source"] == "scene_partition"
    assert (tl_b["source_start"], tl_b["source_end"]) == (22.5, 26.0)
    assert tl_a["target_duration"] == 2.5 and tl_b["target_duration"] == 3.5            # NO 9.9 (target legacy inflado)
    assert abs(tl_a["target_duration"] + tl_b["target_duration"] - 6.0) < 1e-6
    single = vj.compute_timeline(scene, a, [a])
    assert single["target_duration"] == 6.0 and single["timeline_source"] == "scene"
    legacy = vj.compute_timeline({}, {"target": 5.5}, [{"target": 5.5}])
    assert legacy["timeline_source"] == "legacy_target" and legacy["target_duration"] == 5.5


def test_short_provider_clip_is_rejected_and_retried_with_more_duration(monkeypatch):
    """Solo si el material es MENOR que el tramo (no por ser de 8 s): QUALITY_REJECTED(duration_short) → una regeneracion."""
    dub, goog = install(monkeypatch, Fake(script=[ok(video=make_video(2)), None]))
    pid = make_project(dur=5.0, target=4.0)
    res, _ = run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and len(dub.calls) == 2
    assert f5["attempts"][0]["status"] == "REJECTED" and f5["attempts"][0]["error_type"] == "QUALITY_REJECTED"
    assert (store.pdir(pid) / f5["attempts"][0]["raw"]).exists()                    # el RAW rechazado (pagado) NO se borra


# ================================================================== concurrencia: UNA autoridad, slots solo para SUBMIT→POLL→RAW
def test_concurrency_never_exceeds_max_concurrent_video_jobs(monkeypatch):
    vj._overrides["max_concurrent_video_jobs"] = 2
    dub, _ = install(monkeypatch, Fake(delay=0.3))
    pid = make_project(scenes=6)
    res, _ = run(pid)
    assert res["state"] == "COMPLETED" and dub.peak <= 2 and vj.SCHED.slots.peak <= 2 and dub.peak == 2


def test_generation_slot_is_released_when_raw_is_saved_not_after_voice_unify_audit(monkeypatch):
    """MAX_CONCURRENT_VIDEO_JOBS=1 y un cambio de voz LENTO: el 2º clip debe enviarse mientras el 1º aun cambia la voz."""
    vj._overrides["max_concurrent_video_jobs"] = 1
    dub, _ = install(monkeypatch, Fake(delay=0.05))
    voice = {"start": [], "end": [], "slots_used": []}

    def slow_voice(url, vid, progress=None, audio_path=None):
        voice["start"].append(time.time())
        voice["slots_used"].append(vj.SCHED.slots.used)
        time.sleep(1.2)
        voice["end"].append(time.time())
        return mp3(4)

    monkeypatch.setattr(dubvoice, "voice_change", slow_voice)
    pid = make_project(scenes=2, unify=True)
    res, _ = run(pid)
    assert res["state"] == "COMPLETED" and len(dub.calls) == 2
    first_voice_end = min(voice["end"])
    assert dub.calls[1]["t"] < first_voice_end - 0.5, "el slot no se libero antes de la voz/unify del primer clip"
    assert vj.SCHED.slots.peak == 1 and vj.SCHED.slots.used == 0
    assert 0 in voice["slots_used"] or min(voice["slots_used"]) <= 1


# ================================================================== timeouts reales: ningun worker queda secuestrado
def test_hung_provider_call_cannot_hijack_a_worker(monkeypatch):
    """Un proveedor que se cuelga (ignora cancel) es cortado por el watchdog; el slot se libera y el lote sigue."""
    vj._overrides.update(max_concurrent_video_jobs=1, submission_timeout=0.4, processing_timeout=0.4, download_timeout=0.4,
                         download_retries=1, watchdog_margin=0.4)
    release = threading.Event()
    dub, _ = install(monkeypatch, Fake(script=[hang_after_submit(release)]))
    pid = make_project(scenes=2)
    t0 = time.time()
    res, _ = run(pid)
    release.set()
    took = time.time() - t0
    assert took < 25, f"el lote tardo {took:.0f}s: un worker quedo secuestrado"
    assert res["accepted"] == 2
    hung = [c["f5"]["attempts"][0] for c in clips_of(pid) if c["f5"]["attempts"][0]["status"] == "ABANDONED"]
    assert len(hung) == 1 and hung[0]["error_type"] == "PROVIDER_TIMEOUT" and hung[0]["job_id"] and hung[0]["abandoned_job_may_still_bill"]


def test_a_clip_that_hits_its_limit_goes_to_needs_review_and_the_batch_continues(monkeypatch):
    vj._overrides["fallback_provider"] = "none"
    dub, _ = install(monkeypatch)
    dub.by_prompt["prompt s0c0"] = fail_after_submit(F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino en 12 min", provider="dubvoice"))
    pid = make_project(scenes=3)
    res, logs = run(pid)
    bad = f5_of(pid, 0)
    assert bad["state"] == "NEEDS_REVIEW" and bad["review_reason"] == "PROVIDER_TIMEOUT" and paid_count(bad) == 2
    assert clips_of(pid)[0]["status"] == "error" and clips_of(pid)[0]["error"]
    assert [c["f5"]["state"] for c in clips_of(pid)[1:]] == ["ACCEPTED", "ACCEPTED"]
    assert res["state"] == "COMPLETED_WITH_WARNINGS"


def paid_count(f5):
    return vj.paid_attempts(f5)


def test_run_bounded_abandons_uninterruptible_calls_and_honors_cancel():
    t0 = time.time()
    with pytest.raises(F5Error) as e:
        vj.run_bounded(lambda: time.sleep(5), 0.3, None, "prueba")
    assert time.time() - t0 < 1.5 and e.value.etype == ErrorType.PROVIDER_TIMEOUT
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    t0 = time.time()
    with pytest.raises(errors.Cancelled):
        vj.run_bounded(lambda: time.sleep(5), 10, ev, "prueba")
    assert time.time() - t0 < 1.5


def _server(handler_cls):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_http_request_once_and_download_respect_deadline_and_cancel():
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            if self.path == "/silent":
                time.sleep(3)
                return
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            for _ in range(200):
                try:
                    self.wfile.write(b"x" * 10)
                    self.wfile.flush()
                except Exception:
                    return
                time.sleep(0.05)

    srv, base = _server(H)
    try:
        t0 = time.time()
        with pytest.raises(F5Error) as e:                              # el servidor no responde: timeout de lectura
            http.request_once("GET", base + "/silent", read=0.4, deadline=1)
        assert time.time() - t0 < 2 and e.value.etype == ErrorType.CONNECTION_ERROR and e.value.ambiguous
        t0 = time.time()
        with pytest.raises(F5Error) as e:                              # gotea bytes: manda el deadline total, no el timeout por trozo
            http.download(base + "/trickle", deadline=0.6, read=5)
        assert time.time() - t0 < 2 and e.value.etype == ErrorType.DOWNLOAD_ERROR and e.value.sub == "deadline"
        ev = threading.Event()
        threading.Timer(0.3, ev.set).start()
        t0 = time.time()
        with pytest.raises(errors.Cancelled):
            http.download(base + "/trickle", deadline=30, cancel=ev)
        assert time.time() - t0 < 2
        with pytest.raises(F5Error) as e:                              # nadie escucha: fallo ANTES de enviar (no ambiguo)
            http.request_once("POST", "http://127.0.0.1:1/x", json={}, connect=1, deadline=2)
        assert not e.value.ambiguous
    finally:
        srv.shutdown()


# ================================================================== politica por tipo de error
def test_rate_limit_backs_off_with_global_cooldown_and_does_not_consume_an_attempt(monkeypatch):
    dub, _ = install(monkeypatch, Fake(script=[fail_before_submit(F5Error(ErrorType.RATE_LIMIT, "429", retry_after=0.6, http_status=429))]))
    pid = make_project()
    t0 = time.time()
    res, logs = run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and f5["rate_limit_hits"] == 1 and len(dub.calls) == 2
    assert paid_count(f5) == 1 and time.time() - t0 >= 0.6                     # respeto Retry-After; el intento 429 no se paga
    assert f5["attempts"][0]["error_type"] == "RATE_LIMIT" and not f5["attempts"][0]["paid"]
    assert vj.SCHED.rate.cool_until.get("dubvoice", 0) > 0 and any("limite de peticiones" in x for x in logs)
    assert CLAUDE_CALLS == ["QC VISUAL"]                                        # Claude NO decide rate limits: solo hizo el QC


def test_connection_error_before_sending_retries_same_provider(monkeypatch):
    dub, goog = install(monkeypatch, Fake(script=[fail_before_submit(F5Error(ErrorType.CONNECTION_ERROR, "no route", ambiguous=False))]))
    pid = make_project()
    run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and f5["safe_conn_retries"] == 1 and len(dub.calls) == 2 and not goog.calls and paid_count(f5) == 1


def test_ambiguous_submit_is_never_resent_automatically(monkeypatch):
    """POST enviado + reset antes de recibir job_id → NEEDS_REVIEW, cero reenvios, possible_duplicate."""
    dub, goog = install(monkeypatch, Fake(script=[fail_before_submit(F5Error(ErrorType.CONNECTION_ERROR, "Connection reset by peer", ambiguous=True))]))
    pid = make_project(scenes=2)
    res, logs = run(pid)
    bad = [c for c in clips_of(pid) if c["f5"]["state"] == "NEEDS_REVIEW"]
    assert len(bad) == 1 and len(dub.calls) == 2 and not goog.calls              # 1 POST ambiguo + el POST del otro clip; ningun reenvio
    f5 = bad[0]["f5"]
    assert f5["review_reason"] == "AMBIGUOUS_SUBMIT" and f5["possible_duplicate"] and f5["remote_unknown"]
    assert f5["attempts"][0]["possible_duplicate"] and f5["attempts"][0]["status"] == "AMBIGUOUS" and f5["review_kind"] == "REMOTE"
    assert bad[0]["status"] == "error" and "AMBIGUOUS_SUBMIT" in bad[0]["error"]
    assert res["state"] == "COMPLETED_WITH_WARNINGS" and res["possible_duplicates"]


def test_decide_ambiguous_retry_is_opt_in_and_limits_are_enforced():
    cfg = vj.config()
    ctx = vj.Ctx(cfg, "dubvoice", True, "google", 7500)
    f5 = vj._blank_f5()
    amb = F5Error(ErrorType.CONNECTION_ERROR, "reset", ambiguous=True)
    assert cfg.ambiguous_submit_retries == 0 and vj.decide(f5, amb, ctx).kind == "review"
    vj._overrides["ambiguous_submit_retries"] = 1
    ctx1 = vj.Ctx(vj.config(), "dubvoice", True, "google", 7500)
    d = vj.decide(f5, amb, ctx1)
    assert d.kind == "retry" and d.consumes and d.updates["possible_duplicate"]
    # topes: intentos, costo y tiempo total
    f5["attempts"] = [{"id": f"a{i}", "paid": True, "round": 1} for i in range(3)]
    assert vj.decide(f5, F5Error(ErrorType.UNKNOWN_ERROR, "x"), ctx).reason == "MAX_ATTEMPTS"
    f5 = vj._blank_f5()
    f5["credits_spent"] = 20000
    assert vj.decide(f5, F5Error(ErrorType.UNKNOWN_ERROR, "x"), ctx).reason == "MAX_COST"
    f5 = vj._blank_f5()
    f5["gen_seconds"] = 1300
    assert vj.decide(f5, F5Error(ErrorType.UNKNOWN_ERROR, "x"), ctx).reason == "MAX_TOTAL_TIME"
    assert vj.config().max_total_generation_time == 1200          # 20 min por defecto (no 40)


def test_provider_timeout_retries_primary_then_falls_back_to_google(monkeypatch):
    to = F5Error(ErrorType.PROVIDER_TIMEOUT, "DubVoice (video) no termino en 12 min", provider="dubvoice")
    dub, goog = install(monkeypatch, Fake(script=[fail_after_submit(to), fail_after_submit(to)]))
    pid = make_project()
    res, _ = run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and len(dub.calls) == 2 and len(goog.calls) == 1
    assert [a["status"] for a in f5["attempts"]] == ["ABANDONED", "ABANDONED", "VALIDATED"]
    assert [a["provider"] for a in f5["attempts"]] == ["dubvoice", "dubvoice", "google"] and clips_of(pid)[0]["provider_used"] == "google"
    assert all(a["job_id"] for a in f5["attempts"])                              # los job_id de los intentos abandonados se conservan
    assert CLAUDE_CALLS == ["QC VISUAL"]


def test_content_filter_never_resends_the_same_prompt(monkeypatch):
    cf = F5Error(ErrorType.CONTENT_FILTER, "content policy violation", provider="dubvoice")
    dub, goog = install(monkeypatch, Fake(script=[fail_before_submit(cf), fail_before_submit(cf)]))
    pid = make_project()
    run(pid)
    prompts = [c["prompt"] for c in dub.calls] + [c["prompt"] for c in goog.calls]
    assert len(prompts) == 3 and len(set(prompts)) == 3 and prompts[0] == "prompt s0c0"
    assert [p for p in prompts[1:] if not p.startswith("rewritten prompt")] == []
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and [a["provider"] for a in f5["attempts"]] == ["dubvoice", "dubvoice", "google"]
    assert clips_of(pid)[0]["video_prompt"].startswith("rewritten prompt")
    assert CLAUDE_CALLS.count("REESCRIBE PROMPT") == 2


def test_content_filter_with_no_possible_rewrite_goes_to_review_without_resending(monkeypatch):
    def broken(content, **k):
        raise RuntimeError("Anthropic 529")
    cf = F5Error(ErrorType.CONTENT_FILTER, "content policy", provider="dubvoice")
    dub, goog = install(monkeypatch, Fake(script=[fail_before_submit(cf)]))
    monkeypatch.setattr(claude, "ask_json", broken)
    pid = make_project()
    run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "PROMPT_REWRITE_UNAVAILABLE" and len(dub.calls) == 1 and not goog.calls


def test_no_automatic_low_quality_fallback_ladder_or_still_image(monkeypatch):
    """Todos los intentos de video fallan → NEEDS_REVIEW. Nada de escalera lite/omniflash/meta ni locucion sobre imagen fija."""
    to = F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino", provider="x")
    dub, goog = install(monkeypatch, Fake(script=[fail_after_submit(to)] * 6), Fake("google", script=[fail_after_submit(to)] * 6))
    monkeypatch.setattr(videos, "make_still_clip", lambda *a, **k: (_ for _ in ()).throw(AssertionError("imagen fija automatica")))
    pid = make_project()
    res, _ = run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and res["state"] == "COMPLETED_WITH_WARNINGS"
    used = {c["model"] for c in dub.calls} | {c["model"] for c in goog.calls}
    assert used <= {"veo-3.1-fast", google_veo.DEFAULT_MODEL}, used
    assert not any(c.get("provider_used") == "still" for c in clips_of(pid)) and not clips_of(pid)[0].get("file")


def test_google_quota_disables_google_for_the_run_without_a_model_ladder(monkeypatch):
    to = F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino", provider="dubvoice")
    quota = F5Error(ErrorType.PROVIDER_REJECTED, "Google Veo respondio 429: RESOURCE_EXHAUSTED", provider="google", sub="quota", fatal=True)
    vj._overrides["max_concurrent_video_jobs"] = 1
    dub, goog = install(monkeypatch, Fake(script=[fail_after_submit(to)] * 8), Fake("google", script=[fail_before_submit(quota)] * 4))
    pid = make_project(scenes=2)
    res, logs = run(pid)
    assert len(goog.calls) == 1                                                  # una sola vez: luego queda desactivado en la corrida
    assert all(c["f5"]["state"] == "NEEDS_REVIEW" for c in clips_of(pid))
    assert {c["model"] for c in dub.calls} == {"veo-3.1-fast"} and any("desactivado" in x for x in logs)


# ================================================================== audio ≠ video: el audio NUNCA regenera el video
def test_A_visual_ok_but_wrong_audio_keeps_raw_and_never_regenerates(monkeypatch):
    dub, _ = install(monkeypatch)
    monkeypatch.setattr(stt, "transcribe", lambda audio, settings=None, language_code=None: {"text": "completely unrelated words here", "words": []})
    pid = make_project()
    run(pid)
    c = clips_of(pid)[0]
    f5 = c["f5"]
    assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK" and f5["audio_state"] == "NEEDS_FIX" and "no coincide" in f5["audio_issue"]
    assert len(dub.calls) == 1 and paid_count(f5) == 1 and c["status"] == "done" and (store.pdir(pid) / c["raw"]).exists()
    assert "Audio por revisar" in c["warning"]


def test_B_voice_change_timeout_keeps_raw_frees_the_slot_and_never_regenerates(monkeypatch):
    vj._overrides["post_timeout"] = 1.0
    dub, _ = install(monkeypatch)
    seen = {}

    def hang_voice(url, vid, progress=None, audio_path=None):
        seen["slots_used"] = vj.SCHED.slots.used                                 # el slot de generacion ya esta libre durante la voz
        time.sleep(6)
        return mp3(4)

    monkeypatch.setattr(dubvoice, "voice_change", hang_voice)
    pid = make_project(unify=True)
    t0 = time.time()
    run(pid)
    c = clips_of(pid)[0]
    f5 = c["f5"]
    assert seen["slots_used"] == 0 and time.time() - t0 < 5
    assert f5["state"] == "ACCEPTED" and f5["audio_state"] == "NEEDS_FIX" and "voz" in f5["audio_issue"]
    assert len(dub.calls) == 1 and (store.pdir(pid) / c["raw"]).exists() and c["file"] == c["raw"]     # usa el RAW (voz original de Veo)
    assert "unificar" in c["warning"]


def test_C_whisper_failure_keeps_raw_and_does_not_regenerate(monkeypatch):
    dub, _ = install(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("whisper model missing")

    monkeypatch.setattr(stt, "transcribe", boom)
    pid = make_project()
    run(pid)
    c = clips_of(pid)[0]
    assert c["f5"]["state"] == "ACCEPTED" and c["f5"]["audio_state"] == "UNVERIFIED" and len(dub.calls) == 1
    assert (store.pdir(pid) / c["raw"]).exists() and c["status"] == "done"


def test_D_claude_qc_api_failure_keeps_raw_and_resumes_from_raw_without_paying(monkeypatch):
    dub, _ = install(monkeypatch)
    state = {"down": True}

    def flaky(content, **k):
        if state["down"]:
            raise RuntimeError("Anthropic respondio 529")
        return default_claude(content, **k)

    monkeypatch.setattr(claude, "ask_json", flaky)
    pid = make_project()
    res, _ = run(pid)
    c = clips_of(pid)[0]
    f5 = c["f5"]
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "QC_UNAVAILABLE" and f5["review_kind"] == "POST"
    raw = store.pdir(pid) / f5["attempts"][0]["raw"]
    assert raw.exists() and len(dub.calls) == 1 and res["state"] == "COMPLETED_WITH_WARNINGS"
    state["down"] = False                                                        # Claude vuelve → se reanuda DESDE EL RAW
    res2, logs = run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and len(dub.calls) == 1 and paid_count(f5) == 1 and raw.exists()
    assert any("desde el RAW" in x for x in logs) and res2["state"] == "COMPLETED"


def test_visual_rejection_is_the_only_thing_that_regenerates_video(monkeypatch):
    dub, _ = install(monkeypatch)
    calls = {"qc": 0}

    def qc_then_ok(content, **k):
        txt = content[-1]["text"]
        if txt.startswith("QC VISUAL"):
            calls["qc"] += 1
            return {"visual_ok": calls["qc"] > 1, "reasons": ["persona distinta"], "prompt_fix": "prompt corregido por QC"}
        return default_claude(content, **k)

    monkeypatch.setattr(claude, "ask_json", qc_then_ok)
    pid = make_project()
    run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "ACCEPTED" and [c["prompt"] for c in dub.calls] == ["prompt s0c0", "prompt corregido por QC"]
    assert f5["attempts"][0]["status"] == "REJECTED" and paid_count(f5) == 2


# ================================================================== cerrar / reabrir la app
def _restart():
    """Simula reiniciar la app: proceso nuevo (planificador vacio) + jobs.reset_stale() como hace app.main al arrancar."""
    vj.SCHED.reset_for_tests()
    jobs.reset_stale()


def test_F_restart_with_accepted_clips_makes_zero_new_posts(monkeypatch):
    dub, goog = install(monkeypatch)
    pid = make_project(scenes=3)
    run(pid)
    assert len(dub.calls) == 3
    n_claude = len(CLAUDE_CALLS)
    _restart()
    res, logs = run(pid)
    assert len(dub.calls) == 3 and not dub.resumes and not goog.calls and len(CLAUDE_CALLS) == n_claude
    assert res["state"] == "COMPLETED" and all(c["f5"]["state"] == "ACCEPTED" for c in clips_of(pid))
    res, _ = run(pid, explicit=False)                                            # tampoco con otra corrida automatica
    assert len(dub.calls) == 3


def _fake_inflight_job(pid, provider="dubvoice", job="dubvoice-job-77", polled=False):
    """Deja el clip como lo dejaria una app que murio mientras el proveedor generaba: SUBMITTED/PROCESSING con job_id persistido."""
    att = vj.new_attempt(pid, 0, 0, provider=provider, model="veo-3.1-fast", prompt="prompt s0c0", asked=8, credits=7500)
    vj.patch_attempt(pid, 0, 0, att["id"], job_id=job, status="SUBMITTED", submitted_at=time.time(), paid=True)
    vj.transition(pid, 0, 0, vj.SUBMITTED)
    if polled:
        vj.transition(pid, 0, 0, vj.PROCESSING)
    return att["id"]


def test_G_restart_with_known_job_id_reconciles_without_any_new_post(monkeypatch):
    dub, goog = install(monkeypatch)
    pid = make_project()
    _fake_inflight_job(pid, polled=True)
    _restart()
    c = clips_of(pid)[0]
    assert c["f5"]["needs_reconcile"] and c["status"] == "error" and c["f5"]["state"] == "PROCESSING"     # no se resetea a pending
    res, logs = run(pid)
    f5 = f5_of(pid)
    assert dub.calls == [] and goog.calls == [] and dub.resumes == ["dubvoice-job-77"]                        # CERO POST de creacion
    assert f5["state"] == "ACCEPTED" and paid_count(f5) == 1 and f5["attempts"][0]["job_id"] == "dubvoice-job-77"
    assert any("Reconciliando" in x for x in logs) and clips_of(pid)[0]["task_id"] == "dubvoice-job-77"


def test_G2_google_operation_is_reconciled_through_resume(monkeypatch):
    dub, goog = install(monkeypatch)
    pid = make_project()
    _fake_inflight_job(pid, provider="google", job="models/veo/operations/abc")
    _restart()
    res, _ = run(pid)
    assert goog.calls == [] and dub.calls == [] and goog.resumes == ["models/veo/operations/abc"] and f5_of(pid)["state"] == "ACCEPTED"


def test_G3_unverifiable_remote_job_goes_to_review_not_to_a_new_payment(monkeypatch):
    bad = F5Error(ErrorType.INVALID_RESPONSE, "ninguna ruta de sondeo conocida responde", provider="dubvoice", sub="poll_endpoint")
    dub, goog = install(monkeypatch)
    dub.resume_script = [lambda ctx: (_ for _ in ()).throw(bad)] * 2
    pid = make_project()
    _fake_inflight_job(pid, polled=True)
    _restart()
    res, _ = run(pid)
    f5 = f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "UNKNOWN_REMOTE_STATE" and f5["remote_unknown"]
    assert dub.calls == [] and goog.calls == [] and f5["attempts"][0]["job_id"] == "dubvoice-job-77"
    assert "dubvoice-job-77" in json.dumps(res["needs_review_list"])                      # el reporte muestra el job_id para revisarlo a mano
    res, _ = run(pid)                                                                     # 2ª corrida automatica: un intento de recuperacion GRATIS mas
    res, _ = run(pid)                                                                     # 3ª: ya no insiste ni PAGA nada por su cuenta
    assert dub.calls == [] and len(dub.resumes) == 2 and f5_of(pid)["state"] == "NEEDS_REVIEW"


def test_H_restart_with_submitting_and_no_job_id_goes_to_review_with_zero_posts(monkeypatch):
    dub, goog = install(monkeypatch)
    pid = make_project()
    vj.new_attempt(pid, 0, 0, provider="dubvoice", model="veo-3.1-fast", prompt="prompt s0c0", asked=8, credits=7500)   # SUBMITTING, sin job_id
    _restart()
    f5 = f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "AMBIGUOUS_SUBMIT" and f5["possible_duplicate"]
    res, _ = run(pid)
    assert dub.calls == [] and goog.calls == [] and f5_of(pid)["state"] == "NEEDS_REVIEW"
    # Regenerar a secas (lo que hace el boton) tampoco paga: hay un envio dudoso
    res, _ = run(pid, keys=[(0, 0)], explicit=True)
    assert dub.calls == [] and f5_of(pid)["state"] == "NEEDS_REVIEW"
    # ...solo una decision EXPLICITA y consciente (paid=1) abre una ronda nueva pagada
    res, _ = run(pid, keys=[(0, 0)], explicit=True, paid=True)
    assert len(dub.calls) == 1 and f5_of(pid)["state"] == "ACCEPTED" and f5_of(pid)["round"] == 2


def test_interrupted_validation_resumes_from_the_saved_raw_for_free(monkeypatch):
    dub, _ = install(monkeypatch)
    pid = make_project()
    run(pid)
    f5 = f5_of(pid)
    # simula: la app murio despues de guardar el RAW y antes de aceptar
    with store.edit(pid) as q:
        c = q["scenes"][0]["clips"][0]
        c["f5"]["state"], c["status"], c["file"] = "VALIDATING", "running", None
    _restart()
    res, logs = run(pid)
    assert len(dub.calls) == 1 and not dub.resumes and f5_of(pid)["state"] == "ACCEPTED" and clips_of(pid)[0]["file"]


def test_stop_cancels_without_losing_the_remote_job(monkeypatch):
    release = threading.Event()
    dub, _ = install(monkeypatch, Fake(script=[hang_after_submit(release)]))
    pid = make_project()
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("res", supervisor.start(pid, lambda msg=None, progress=None: None)), daemon=True)
    t.start()
    end = time.time() + 10
    while time.time() < end and not first_job_id(pid):
        time.sleep(0.02)
    t0 = time.time()
    supervisor.stop(pid)
    t.join(10)
    assert not t.is_alive() and time.time() - t0 < 5
    f5 = f5_of(pid)
    assert f5["state"] in ("SUBMITTED", "PROCESSING") and f5["needs_reconcile"] and f5["attempts"][0]["job_id"] == "dubvoice-job-0"
    assert clips_of(pid)[0]["status"] == "error" and len(dub.calls) == 1
    release.set()
    _restart()
    res, _ = run(pid)                                                                     # al reanudar: reconcilia, no re-paga
    assert len(dub.calls) == 1 and dub.resumes == ["dubvoice-job-0"] and f5_of(pid)["state"] == "ACCEPTED"


# ================================================================== estados globales
def test_11_accepted_plus_1_needs_review_ends_completed_with_warnings(monkeypatch):
    vj._overrides.update(fallback_provider="none", max_concurrent_video_jobs=4)
    dub, _ = install(monkeypatch)
    dub.by_prompt["prompt s5c0"] = fail_after_submit(F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino en 12 min", provider="dubvoice"))
    pid = make_project(scenes=12)
    res = supervisor.start(pid, lambda msg=None, progress=None: None)                    # NO lanza excepcion: no es un error
    states = [c["f5"]["state"] for c in clips_of(pid)]
    assert states.count("ACCEPTED") == 11 and states.count("NEEDS_REVIEW") == 1 and states[5] == "NEEDS_REVIEW"
    assert res["state"] == "COMPLETED_WITH_WARNINGS" and store.get(pid)["f5_run"]["state"] == "COMPLETED_WITH_WARNINGS"
    assert res["needs_review_list"][0]["scene"] == 6 and res["needs_review_list"][0]["job_ids"]
    # contrato con F6: los 11 estan 'done' con archivo; el pendiente esta en 'error' (F6 sigue exigiendo todos: no se salta escenas)
    assert [c["status"] for c in clips_of(pid)].count("done") == 11 and clips_of(pid)[5]["status"] == "error"
    assert vj.global_state(store.get(pid)) == "COMPLETED_WITH_WARNINGS"
    log = " ".join(x["msg"] for x in store.get(pid)["jobs"]["supervisor"]["log"])
    assert "Terminado: 11/12" in log and "COMPLETED_WITH_WARNINGS" in log


def test_autopilot_stops_before_f6_when_f5_has_warnings():
    src = (Path(__file__).resolve().parents[1] / "app" / "autopilot.py").read_text()
    assert "COMPLETED_WITH_WARNINGS" in src and "no se arma con clips faltantes" in src


def test_accepted_clips_are_never_paid_again_unless_explicit_or_stale(monkeypatch):
    dub, _ = install(monkeypatch)
    pid = make_project(scenes=2)
    run(pid)
    run(pid)
    assert len(dub.calls) == 2
    with store.edit(pid) as q:
        q["scenes"][1]["clips"][0]["stale"] = True                                        # el usuario edito el prompt (F4)
    run(pid)
    assert len(dub.calls) == 3 and f5_of(pid, 1)["round"] == 2 and f5_of(pid, 0)["round"] == 1
    run(pid, keys=[(0, 0)], explicit=True)                                                # Regenerar explicito
    assert len(dub.calls) == 4 and f5_of(pid, 0)["round"] == 2


def test_all_clips_failed_by_precondition_is_global_failed(monkeypatch):
    dub, _ = install(monkeypatch)
    pid = make_project(scenes=2)
    with store.edit(pid) as q:
        for s in q["scenes"]:
            s["image"]["approved"] = False
    res, _ = run(pid)
    assert res["state"] == "FAILED" and all(c["f5"]["state"] == "FAILED" for c in clips_of(pid)) and not dub.calls
    with pytest.raises(RuntimeError):
        supervisor.start(pid, lambda msg=None, progress=None: None)


# ================================================================== contrato canario de DubVoice
def test_canary_first_job_runs_alone_until_the_contract_is_verified(monkeypatch):
    vj._overrides.update(contract_canary=True, max_concurrent_video_jobs=3)
    dub, _ = install(monkeypatch, Fake(delay=0.25))
    monkeypatch.setattr(dubvoice, "contract_verified", lambda: False)
    pid = make_project(scenes=3)
    run(pid)
    assert dub.peak == 1                                                                  # sin contrato verificado NO se abre concurrencia
    vj.SCHED.reset_for_tests()
    dub2, _ = install(monkeypatch, Fake(delay=0.25))
    monkeypatch.setattr(dubvoice, "contract_verified", lambda: True)
    pid2 = make_project(scenes=3)
    run(pid2)
    assert dub2.peak == 3


def test_contract_mismatch_stops_all_new_submissions(monkeypatch):
    """Si el contrato no coincide: DETENER nuevos envios (no gastar mas creditos adivinando), ni siquiera al fallback."""
    vj._overrides.update(contract_canary=True)
    bad = F5Error(ErrorType.INVALID_RESPONSE, "DubVoice (video) no devolvio id de tarea", provider="dubvoice", ambiguous=True, sub="no_job_id")
    dub, goog = install(monkeypatch, Fake(script=[fail_before_submit(bad)] * 9))
    monkeypatch.setattr(dubvoice, "contract_verified", lambda: False)
    pid = make_project(scenes=4)
    res, logs = run(pid)
    assert len(dub.calls) == 1 and not goog.calls                                         # UN solo POST en total
    reasons = sorted(c["f5"]["review_reason"] for c in clips_of(pid))
    assert reasons == ["AMBIGUOUS_SUBMIT", "PROVIDER_FROZEN", "PROVIDER_FROZEN", "PROVIDER_FROZEN"]
    assert res["state"] == "COMPLETED_WITH_WARNINGS" and any("DETENGO" in x for x in logs)


# ================================================================== adaptador DubVoice (camino estricto) con HTTP simulado
CANARY = "sk_live_CANARYSECRET1234567890"


class R:
    def __init__(self, code=200, j=None, headers=None, text=None):
        self.status_code, self._j = code, j
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = text if text is not None else json.dumps(j)
        self.content = self.text.encode()

    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j


class Net:
    """HTTP simulado para request_once / http_download de dubvoice y google_veo. Registra cada peticion."""

    def __init__(self, post=None, polls=None, download=None):
        self.reqs: list[tuple] = []
        self.post = post or (lambda body: R(200, {"task_id": "T-1"}))
        self.polls = list(polls or [R(200, {"status": "processing"}), R(200, {"status": "completed", "video_url": "https://cdn.x/v.mp4?sig=SECRETSIG&exp=1"})])
        self.download = download or (lambda url: b"MP4BYTES")

    def request_once(self, method, url, **kw):
        self.reqs.append((method, url, kw))
        if method == "POST":
            r = self.post(kw.get("json"))
            if isinstance(r, Exception):
                raise r
            return r
        r = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(r, Exception):
            raise r
        return r

    def http_download(self, url, **kw):
        self.reqs.append(("DOWNLOAD", url, kw))
        d = self.download(url)
        if isinstance(d, Exception):
            raise d
        return d


LIM = {"submission": 2, "poll_request": 1, "processing": 3, "download": 2, "download_retries": 2, "poll_interval": 0.02, "max_poll_failures": 3}


def net_install(monkeypatch, net: Net, mod=dubvoice):
    monkeypatch.setattr(mod, "request_once", net.request_once)
    monkeypatch.setattr(mod, "http_download", net.http_download)
    monkeypatch.setenv("DUBVOICE_API_KEY", CANARY)
    dubvoice._poll_cache.clear()


def strict_veo(tmp_path, **kw):
    img = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64)).save(img)
    return dubvoice.veo("prompt", img, limits={**LIM, **kw.pop("limits", {})}, **kw)


def test_dubvoice_strict_flow_persists_job_id_first_records_contract_and_verifies_it(monkeypatch, tmp_path):
    net = Net(post=lambda body: R(200, {"data": {"task_id": "T-42"}, "echo_key": CANARY}, {"x-ratelimit-remaining": "9", "authorization": CANARY}))
    net_install(monkeypatch, net)
    monkeypatch.setattr(dubvoice, "contract_file", lambda: tmp_path / "contracts" / "dubvoice.json")
    pid = make_project()
    rec = dubvoice.ContractRecorder(pid, "dubvoice", 0, 0, "a1")
    order = []
    tid, data = strict_veo(tmp_path, on_submit=lambda j, m=None: order.append(("submit", j, m)), on_poll=lambda n, s: order.append(("poll", n, s)),
                           recorder=rec)
    assert (tid, data) == ("T-42", b"MP4BYTES")
    assert order[0] == ("submit", "T-42", {"resolved_by": "data.task_id"}) and order[1][0] == "poll"        # on_submit ANTES del primer sondeo
    posts = [r for r in net.reqs if r[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["json"]["ref_images"][0].startswith("data:image/jpeg;base64,")
    assert "cancel" in posts[0][2] and posts[0][2]["deadline"] == 2                                          # timeouts reales en cada peticion
    contract = json.loads((tmp_path / "contracts" / "dubvoice.json").read_text())
    assert contract["verified"] and contract["poll_endpoint"] == "/api/v1/video" and contract["submit_id_field"] == "data.task_id"
    assert contract["statuses_seen"] == ["processing", "completed"] and contract["result_field"] == ["video_url"]
    lines = [json.loads(x) for x in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines()]
    ops = [x["op"] for x in lines]
    assert ops[0] == "submit" and "poll" in ops and "status_transition" in ops and "download" in ops and "CONTRACT_VERIFIED" in ops
    sub = lines[0]
    assert sub["http_status"] == 200 and sub["json_shape"] and sub["headers"].get("x-ratelimit-remaining") == "9" and "authorization" not in sub["headers"]
    everything = "".join(f.read_text() for f in store.pdir(pid).rglob("*") if f.is_file() and f.suffix in (".json", ".jsonl")) + (tmp_path / "contracts" / "dubvoice.json").read_text()
    assert CANARY not in everything and "SECRETSIG" not in everything and "base64," not in everything          # NUNCA claves, firmas ni base64


def test_dubvoice_strict_2xx_without_job_id_is_ambiguous_and_visible(monkeypatch, tmp_path):
    net = Net(post=lambda body: R(200, {"ok": True, "message": "queued"}))
    net_install(monkeypatch, net)
    called = []
    pid = make_project()
    rec = dubvoice.ContractRecorder(pid, "dubvoice", 0, 0, "a1")
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path, on_submit=lambda j, m=None: called.append(j), recorder=rec)
    assert e.value.etype == ErrorType.INVALID_RESPONSE and e.value.ambiguous and e.value.sub == "no_job_id" and not called
    assert len([r for r in net.reqs if r[0] == "POST"]) == 1 and len(net.reqs) == 1                       # se detiene: ni sondea ni reintenta
    first = json.loads((store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines()[0])
    assert first["json_shape"] == {"ok": "bool", "message": "str(6)"}                                       # se ve al instante que forma llego


def test_dubvoice_strict_post_reset_is_ambiguous_and_never_retried_inside(monkeypatch, tmp_path):
    reset = F5Error(ErrorType.CONNECTION_ERROR, "Sin conexion: ReadError", ambiguous=True)
    net = Net(post=lambda body: reset)
    net_install(monkeypatch, net)
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path)
    assert e.value.ambiguous and e.value.provider == "dubvoice" and [r[0] for r in net.reqs] == ["POST"]


def test_dubvoice_strict_429_and_402_and_5xx_and_gateway_are_typed(monkeypatch, tmp_path):
    cases = [(R(429, {"error": "slow down"}, {"Retry-After": "7"}), ErrorType.RATE_LIMIT, False),
             (R(402, {"error": "no credits"}), ErrorType.PROVIDER_REJECTED, False),
             (R(503, {"error": "down"}), ErrorType.CONNECTION_ERROR, False),
             (R(504, {"error": "gateway"}), ErrorType.CONNECTION_ERROR, True),          # el proxy pudo cortar DESPUES de crear el job
             (R(400, {"error": "content policy violation"}), ErrorType.CONTENT_FILTER, False)]
    for resp, etype, amb in cases:
        net_install(monkeypatch, Net(post=lambda body, r=resp: r))
        with pytest.raises(F5Error) as e:
            strict_veo(tmp_path)
        assert e.value.etype == etype and e.value.ambiguous == amb, (resp.status_code, e.value.etype)
        if resp.status_code == 429:
            assert e.value.retry_after == 7
        if resp.status_code == 402:
            assert e.value.fatal and e.value.sub == "credits"


def test_dubvoice_strict_unrecognized_status_is_logged_loudly_and_processing_timeout_keeps_job_id(monkeypatch, tmp_path):
    net = Net(polls=[R(200, {"status": "weird_state"})])
    net_install(monkeypatch, net)
    pid = make_project()
    rec = dubvoice.ContractRecorder(pid, "dubvoice", 0, 0, "a1")
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path, limits={"processing": 0.3}, recorder=rec)
    assert e.value.etype == ErrorType.PROVIDER_TIMEOUT and e.value.job_id == "T-1"
    ops = [json.loads(x) for x in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines()]
    assert any(o["op"] == "UNRECOGNIZED_STATUS" and o["raw_status"] == "weird_state" for o in ops)
    assert not dubvoice.contract_verified()


def test_dubvoice_strict_poll_failures_are_capped_and_never_resubmit(monkeypatch, tmp_path):
    down = F5Error(ErrorType.CONNECTION_ERROR, "Sin conexion", ambiguous=True)
    net = Net(polls=[down])
    net_install(monkeypatch, net)
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path)
    assert e.value.etype == ErrorType.CONNECTION_ERROR and e.value.job_id == "T-1" and e.value.sub == "poll_failures"
    assert len([r for r in net.reqs if r[0] == "POST"]) == 1


def test_dubvoice_strict_provider_failed_is_typed_and_content_filter_detected(monkeypatch, tmp_path):
    net_install(monkeypatch, Net(polls=[R(200, {"status": "failed", "error": "Blocked by safety filters"})]))
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path)
    assert e.value.etype == ErrorType.CONTENT_FILTER and e.value.job_id == "T-1"
    net_install(monkeypatch, Net(polls=[R(200, {"status": "failed", "error": "internal"})]))
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path)
    assert e.value.etype == ErrorType.PROVIDER_REJECTED and e.value.sub == "provider_failed"


def test_dubvoice_strict_download_retries_then_download_error_keeps_the_job(monkeypatch, tmp_path):
    err = F5Error(ErrorType.DOWNLOAD_ERROR, "Descarga respondio 403")
    net = Net(download=lambda url: err)
    net_install(monkeypatch, net)
    with pytest.raises(F5Error) as e:
        strict_veo(tmp_path)
    assert e.value.etype == ErrorType.DOWNLOAD_ERROR and e.value.job_id == "T-1" and "sig=" not in (e.value.result_url or "")
    assert len([r for r in net.reqs if r[0] == "DOWNLOAD"]) == 2 and len([r for r in net.reqs if r[0] == "POST"]) == 1


def test_dubvoice_resume_polls_an_existing_job_and_never_posts(monkeypatch):
    net = Net()
    net_install(monkeypatch, net)
    tid, data = dubvoice.resume("T-9", limits=LIM)
    assert (tid, data) == ("T-9", b"MP4BYTES")
    assert all(r[0] != "POST" for r in net.reqs) and net.reqs[0][2]["params"] == {"task_id": "T-9"}


def test_dubvoice_poll_route_discovery_tries_candidates_and_remembers_the_one_that_answers(monkeypatch):
    net = Net(polls=[R(404, {"error": "nf"}), R(404, {"error": "nf"}), R(200, {"status": "completed", "url": "https://cdn.x/a.mp4"})])
    net_install(monkeypatch, net)
    dubvoice.resume("T-9", limits=LIM)
    urls = [r[1] for r in net.reqs if r[0] == "GET"]
    assert urls[0].endswith("/api/v1/video") and urls[2].endswith("/api/v1/video/status")
    assert dubvoice._poll_cache["/api/v1/video"] == ("/api/v1/video/status", "task_id")


def test_legacy_dubvoice_veo_without_limits_is_unchanged(monkeypatch, tmp_path):
    """Sin `limits` (uso manual) el adaptador se comporta como siempre: usa request()/download() heredados."""
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    seen = []
    monkeypatch.setattr(dubvoice, "request", lambda m, u, **k: (seen.append(m), R(200, {"task_id": "L1"}) if m == "POST" else R(200, {"status": "completed", "result": "https://x/v.mp4"}))[1])
    monkeypatch.setattr(dubvoice, "download", lambda u: b"LEGACY")
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    dubvoice._poll_cache.clear()
    img = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64)).save(img)
    assert dubvoice.veo("p", img) == ("L1", b"LEGACY") and seen[0] == "POST"


# ================================================================== adaptador Google Veo
def test_google_strict_flow_reports_operation_name_and_resume_never_posts(monkeypatch, tmp_path):
    op = "models/veo-3.1/operations/OP-7"
    calls = []

    class GNet(Net):
        def request_once(self, method, url, **kw):
            calls.append((method, url))
            if method == "POST":
                return R(200, {"name": op})
            return R(200, {"done": True, "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": "https://g/files/v:download?alt=media"}}]}}})

    net = GNet()
    monkeypatch.setattr(google_veo, "request_once", net.request_once)
    monkeypatch.setattr(google_veo, "http_download", net.http_download)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    img = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64)).save(img)
    subs = []
    name, data = google_veo.veo("p", img, duration=3, limits=LIM, on_submit=lambda j, m=None: subs.append(j))
    assert name == op and subs == [op] and data == b"MP4BYTES"                                             # nombre COMPLETO (sirve para resume)
    assert [c[0] for c in calls] == ["POST", "GET"] and calls[1][1].endswith(op)
    calls.clear()
    assert google_veo.resume(op, limits=LIM)[0] == op and [c[0] for c in calls] == ["GET"]                 # resume: cero POST


def test_google_quota_429_is_a_fatal_rejection_not_a_rate_limit(monkeypatch, tmp_path):
    class GNet(Net):
        def request_once(self, method, url, **kw):
            return R(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}})
    monkeypatch.setattr(google_veo, "request_once", GNet().request_once)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    img = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64)).save(img)
    with pytest.raises(F5Error) as e:
        google_veo.veo("p", img, limits=LIM)
    assert e.value.etype == ErrorType.PROVIDER_REJECTED and e.value.sub == "quota" and e.value.fatal


# ================================================================== errores tipados y secretos
def test_error_taxonomy_and_classification():
    table = {
        "Google Veo respondio 429: RESOURCE_EXHAUSTED You exceeded your current quota": ErrorType.PROVIDER_REJECTED,
        "x respondio 429: slow down": ErrorType.RATE_LIMIT,
        "DubVoice (video) fallo: content policy violation": ErrorType.CONTENT_FILTER,
        "DubVoice (video) no termino en 10 min": ErrorType.PROVIDER_TIMEOUT,
        "Sin conexion con https://x: [Errno 54] Connection reset by peer": ErrorType.CONNECTION_ERROR,
        "x respondio 503: y": ErrorType.CONNECTION_ERROR,
        "x respondio 402: pay": ErrorType.PROVIDER_REJECTED,
        "DubVoice (video) no devolvio id de tarea": ErrorType.INVALID_RESPONSE,
        "Descarga de resultado respondio 403": ErrorType.DOWNLOAD_ERROR,
        "Falta la API key de dubvoice": ErrorType.PROVIDER_REJECTED,
        "algo totalmente raro": ErrorType.UNKNOWN_ERROR,
    }
    for text, et in table.items():
        assert errors.classify(text) == et, text
    assert {e.value for e in ErrorType} == {"RATE_LIMIT", "CONNECTION_ERROR", "PROVIDER_TIMEOUT", "PROVIDER_REJECTED", "CONTENT_FILTER",
                                            "INVALID_RESPONSE", "DOWNLOAD_ERROR", "QUALITY_REJECTED", "UNKNOWN_ERROR"}
    e = errors.as_f5(RuntimeError("Google Veo respondio 429: RESOURCE_EXHAUSTED quota"), provider="google", job_id="J")
    assert e.etype == ErrorType.PROVIDER_REJECTED and e.sub == "quota" and e.fatal and e.job_id == "J"
    assert set(vj.TRANSITIONS) == set(vj.STATES) == {"PENDING", "SUBMITTED", "PROCESSING", "VALIDATING", "ACCEPTED", "RETRY_PENDING", "FAILED", "NEEDS_REVIEW"}


def test_secrets_never_reach_project_json_events_or_contract_logs(monkeypatch):
    monkeypatch.setenv("DUBVOICE_API_KEY", CANARY)
    boom = RuntimeError(f"DubVoice respondio 401 Unauthorized: bad key {CANARY} Authorization: Bearer {CANARY} https://cdn/x.mp4?token=abc&X-Goog-Signature=zzz")
    dub, _ = install(monkeypatch, Fake(script=[fail_before_submit(boom)]))
    monkeypatch.setattr(dubvoice, "veo", dub.veo)
    vj._overrides["fallback_provider"] = "none"
    pid = make_project()
    res, logs = run(pid)
    blob = "".join(f.read_text(errors="ignore") for f in store.pdir(pid).rglob("*") if f.is_file() and f.suffix in (".json", ".jsonl")) + "".join(logs)
    blob += json.dumps(res, default=str)
    assert CANARY not in blob and "X-Goog-Signature=zzz" not in blob and "token=abc" not in blob
    assert f5_of(pid)["state"] in ("FAILED", "NEEDS_REVIEW")
    assert errors.sanitize({"api_key": "x", "nested": [{"Authorization": "Bearer q"}], "u": "https://a/b?sig=1"}) == {
        "api_key": "<redacted>", "nested": [{"Authorization": "<redacted>"}], "u": "https://a/b?<query-redacted>"}


# ================================================================== telemetria y rutas
def test_telemetry_summary_and_endpoints_route_through_the_single_scheduler(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    to = F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino en 12 min", provider="dubvoice")
    dub, goog = install(monkeypatch, Fake(script=[fail_after_submit(to)]))
    pid = make_project(scenes=2)
    run(pid)
    client = TestClient(app)
    s = client.get(f"/api/projects/{pid}/f5/summary").json()
    assert s["state"] == "COMPLETED" and s["accepted"] == 2 and s["clips_total"] == 2 and s["needs_review"] == 0
    assert s["attempts_total"] == 3 and s["paid_attempts"] == 3 and s["retries"] == 1
    assert s["errors_by_type"] == {"PROVIDER_TIMEOUT": 1} and s["per_provider"]["dubvoice"] == {"attempts": 3, "accepted": 2, "paid": 3}
    assert s["avg_generation_seconds"] is not None and s["max_generation_seconds"] >= s["avg_generation_seconds"] and s["wall_seconds"] >= 0
    assert s["credits_estimated"] == 22500 and len(s["clips"]) == 2 and len(s["clips"][0]["attempts"]) + len(s["clips"][1]["attempts"]) == 3
    assert all(a["job_id"] for c in s["clips"] for a in c["attempts"])
    # regenerar = accion explicita: pasa por el MISMO planificador (slots) y crea una ronda nueva
    n0 = len(dub.calls)
    r = client.post(f"/api/projects/{pid}/videos/0/0/regenerate")
    assert r.status_code == 200
    end = time.time() + 30
    while time.time() < end and client.get(f"/api/projects/{pid}").json()["jobs"].get("clip:0:0", {}).get("status") == "running":
        time.sleep(0.1)
    assert client.get(f"/api/projects/{pid}").json()["jobs"]["clip:0:0"]["status"] == "done"
    assert len(dub.calls) == n0 + 1 and f5_of(pid)["round"] == 2 and vj.SCHED.slots.peak >= 1
    assert client.get(f"/api/projects/{pid}/f5/clip/0/0").json()["state"] == "ACCEPTED"


def test_legacy_project_without_f5_opens_and_done_clips_are_not_repaid(monkeypatch):
    dub, _ = install(monkeypatch)
    pid = make_project(scenes=2)
    f = store.path(pid, "videos", "old.mp4")
    f.write_bytes(make_video(8))
    with store.edit(pid) as q:
        c = q["scenes"][0]["clips"][0]
        c.update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), provider_used="dubvoice", task_id="OLD", verified=True, duration=8.0)
    res, _ = run(pid)
    assert len(dub.calls) == 1                                                            # solo el clip pendiente se genera
    old = clips_of(pid)[0]
    assert "f5" not in old and old["task_id"] == "OLD" and vj.clip_state(old) == "ACCEPTED"     # el proyecto antiguo no se toca
    scene = store.get(pid)["scenes"][0]
    derived = vj.ensure(dict(old), scene, scene["clips"])
    assert derived["state"] == "ACCEPTED" and derived["attempts"][0]["legacy"] is True and derived["audio_state"] == "OK"
    assert f5_of(pid, 1)["state"] == "ACCEPTED" and vj.global_state(store.get(pid)) == "COMPLETED"


def test_configuration_is_central_and_editable_without_code(monkeypatch, tmp_path):
    vj._overrides.clear()
    for k in ("MAX_CONCURRENT_VIDEO_JOBS", "VIDEO_REQUESTS_PER_MINUTE", "MAX_TOTAL_GENERATION_TIME", "PROCESSING_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    cfg = vj.config()
    assert (cfg.max_concurrent_video_jobs, cfg.video_requests_per_minute, cfg.max_attempts_per_clip, cfg.max_total_generation_time,
            cfg.processing_timeout, cfg.ambiguous_submit_retries, cfg.max_cost_per_clip) == (3, 6, 3, 1200, 720, 0, 25000)
    monkeypatch.setenv("MAX_CONCURRENT_VIDEO_JOBS", "1")
    monkeypatch.setenv("PROCESSING_TIMEOUT", "300")
    f = config.DATA_DIR / "f5_config.json"
    f.write_text(json.dumps({"VIDEO_REQUESTS_PER_MINUTE": 4, "MAX_TOTAL_GENERATION_TIME": 900}))
    try:
        cfg = vj.config()
        assert (cfg.max_concurrent_video_jobs, cfg.processing_timeout, cfg.video_requests_per_minute, cfg.max_total_generation_time) == (1, 300, 4, 900)
    finally:
        f.unlink()


def test_rate_gate_enforces_requests_per_minute_and_global_cooldown():
    gate = vj.RateGate()
    gate.acquire("dubvoice", 2, None)
    gate.acquire("dubvoice", 2, None)                                                    # 2 por minuto: la 3ª debe BLOQUEAR
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.time()
    with pytest.raises(errors.Cancelled):
        gate.acquire("dubvoice", 2, ev)
    assert 0.4 < time.time() - t0 < 2
    gate.acquire("google", 2, None)                                                       # el limite es POR proveedor
    g2 = vj.RateGate()
    g2.cooldown("dubvoice", 0.4)
    t0 = time.time()
    g2.acquire("dubvoice", 100, None)
    assert time.time() - t0 >= 0.35                                                       # cooldown global tras un 429


def test_reset_job_endpoint_cancels_the_f5_run_and_keeps_the_remote_job(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    release = threading.Event()
    dub, _ = install(monkeypatch, Fake(script=[hang_after_submit(release)]))
    pid = make_project()
    client = TestClient(app)
    assert client.post(f"/api/projects/{pid}/videos/generate").status_code == 200
    end = time.time() + 10
    while time.time() < end and not first_job_id(pid):
        time.sleep(0.02)
    assert client.post(f"/api/projects/{pid}/jobs/videos/reset").status_code == 200
    end = time.time() + 10
    while time.time() < end and vj.SCHED.active:
        time.sleep(0.05)
    f5 = f5_of(pid)
    assert not vj.SCHED.active and f5["needs_reconcile"] and f5["attempts"][0]["job_id"] == "dubvoice-job-0" and len(dub.calls) == 1
    release.set()


def test_rate_limit_with_an_existing_job_keeps_polling_it_and_never_buys_another():
    cfg = vj.config()
    f5 = vj._blank_f5()
    f5["attempts"] = [{"id": "a1", "paid": True, "round": 1, "job_id": "J1", "provider": "dubvoice"}]
    f5["current_attempt"] = "a1"
    d = vj.decide(f5, F5Error(ErrorType.RATE_LIMIT, "429", retry_after=30, job_id="J1"), vj.Ctx(cfg, "dubvoice", True, "google", 7500))
    assert d.kind == "resume" and d.wait >= 30 and not d.consumes


def test_timeout_goes_straight_to_fallback_when_the_clip_budget_cannot_afford_another_full_window():
    """Con los valores por defecto (12 min de procesamiento, 20 min por clip) tras un timeout solo quedan 8 min: no se repite el mismo proveedor."""
    vj._overrides.clear()
    cfg = vj.config()
    f5 = vj._blank_f5()
    f5["attempts"] = [{"id": "a1", "paid": True, "round": 1, "job_id": "J1", "provider": "dubvoice"}]
    f5["current_attempt"], f5["gen_seconds"] = "a1", 725.0
    err = F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino", job_id="J1")
    d = vj.decide(f5, err, vj.Ctx(cfg, "dubvoice", True, "google", 7500))
    assert d.kind == "retry" and d.provider == "fallback"
    d = vj.decide(f5, err, vj.Ctx(cfg, "dubvoice", False, "", 7500))                    # sin fallback: una ventana corta con el mismo proveedor
    assert d.kind == "retry" and d.provider == "same"
    f5["gen_seconds"] = 20.0                                                            # holgado: primero se reintenta el primario
    assert vj.decide(f5, err, vj.Ctx(cfg, "dubvoice", True, "google", 7500)).provider == "same"


def test_stale_legacy_done_clip_is_regenerated_on_explicit_change(monkeypatch):
    dub, _ = install(monkeypatch)
    pid = make_project()
    f = store.path(pid, "videos", "old.mp4")
    f.write_bytes(make_video(8))
    with store.edit(pid) as q:
        q["scenes"][0]["clips"][0].update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), stale=True, provider_used="dubvoice")
    run(pid)
    assert len(dub.calls) == 1 and f5_of(pid)["state"] == "ACCEPTED" and f5_of(pid)["round"] == 2 and not clips_of(pid)[0]["stale"]


def test_project_setting_video_fallback_false_disables_the_google_fallback(monkeypatch):
    to = F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino", provider="dubvoice")
    dub, goog = install(monkeypatch, Fake(script=[fail_after_submit(to)] * 4))
    pid = make_project()
    with store.edit(pid) as q:
        q["settings"]["video_fallback"] = False
    run(pid)
    assert not goog.calls and f5_of(pid)["state"] == "NEEDS_REVIEW"


def test_f5_report_tool_prints_the_real_test_table_without_secrets(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("f5_report", Path(__file__).resolve().parents[1] / "tools" / "f5_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    to = F5Error(ErrorType.PROVIDER_TIMEOUT, "no termino en 12 min", provider="dubvoice")
    install(monkeypatch, Fake(script=[fail_after_submit(to)]))
    pid = make_project(scenes=2)
    run(pid)
    text = mod.build(pid, {(1, 1), (2, 1)}, 5)         # cual clip sufre el timeout depende del orden de los hilos: se listan ambos
    for needle in ("estado F5: COMPLETED", "dubvoice-job-0", "PROVIDER_TIMEOUT", "ABANDONED", "VALIDATED", "target", "ULTIMOS 5 EVENTOS", "CONTRATO DUBVOICE"):
        assert needle in text, needle
    only_first = mod.build(pid, {(1, 1)}, 0)
    assert "2.1" not in only_first.split("\n\n")[1] and "1.1" in only_first                   # --clips filtra
    assert "d-test-key" not in text and "g-test-key" not in text


def test_configuration_also_reads_exports_from_zshrc_style_files(monkeypatch):
    vj._overrides.clear()
    monkeypatch.delenv("MAX_CONCURRENT_VIDEO_JOBS", raising=False)
    monkeypatch.setitem(config._loaded, "MAX_CONCURRENT_VIDEO_JOBS", "2")            # lo que config.load_env() lee de ~/.zshrc
    assert vj.config().max_concurrent_video_jobs == 2
    monkeypatch.setenv("MAX_CONCURRENT_VIDEO_JOBS", "1")                             # el entorno real manda
    assert vj.config().max_concurrent_video_jobs == 1


def test_legacy_migrated_attempt_is_history_not_a_paid_f5_attempt(monkeypatch):
    """Regresion del canario: un clip 'done' de una version anterior (intento legacy) NO debe contar como paid_attempt / credito / POST de F5."""
    dub, _ = install(monkeypatch)
    pid = make_project(scenes=2)
    f = store.path(pid, "videos", "old.mp4")
    f.write_bytes(make_video(8))
    with store.edit(pid) as q:
        q["scenes"][0]["clips"][0].update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), provider_used=None, task_id="OLD", verified=True)
        q["scenes"][1]["clips"][0]["status"] = "pending"
    with vj.clip_edit(pid, 0, 0):                                                        # persiste el bloque f5 del clip migrado (como en el canario real)
        pass
    res, _ = run(pid, keys=[(0, 0), (1, 0)], explicit=False)
    assert len(dub.calls) == 1                                                          # solo el clip nuevo hizo un POST
    legacy_f5 = vj.ensure(dict(clips_of(pid)[0]), store.get(pid)["scenes"][0], store.get(pid)["scenes"][0]["clips"])
    assert legacy_f5["attempts"][0]["legacy"] and legacy_f5["attempts"][0]["paid"]       # el historial legacy NO se borra ni se altera
    assert vj.paid_attempts(legacy_f5) == 0 and legacy_f5["credits_spent"] == 0
    s = vj.summarize(store.get(pid))
    assert s["attempts_total"] == 1 and s["paid_attempts"] == 1 and s["accepted"] == 2 and s["credits_estimated"] == 7500
    assert "?" not in s["per_provider"] and s["per_provider"] == {"dubvoice": {"attempts": 1, "accepted": 1, "paid": 1}}
    assert any(a.get("legacy") for c in s["clips"] for a in c["attempts"])              # el legacy sigue visible en el detalle
    # regenerar el clip legacy abre una ronda nueva: el legacy no consume presupuesto
    run(pid, keys=[(0, 0)], explicit=True)
    f5 = f5_of(pid, 0)
    assert f5["round"] == 2 and vj.paid_attempts(f5) == 1 and clips_of(pid)[0]["attempts"] == 1

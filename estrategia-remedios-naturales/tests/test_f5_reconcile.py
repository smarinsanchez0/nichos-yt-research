"""PASO 3.2 · AMBIGUOUS_SUBMIT → RECONCILING, contrato real de DubVoice (POST sincrono/timeouts), adopcion manual y telemetria.

Sin red real y sin creditos: proveedores simulados + un servidor HTTP local en 127.0.0.1 que imita el comportamiento observado en el
canario #2 (el POST tarda mas que el timeout de lectura, pero el servidor SI crea el job).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

import test_f5_jobs as T
from app import jobs, media, store
from app.phases import supervisor, video_jobs as vj, videos
from app.services import dubvoice, errors, google_veo
from app.services.errors import ErrorType, F5Error, NotSupported

env = T.env          # fixture autouse (config rapida, sin red, Claude simulado)
AMB = F5Error(ErrorType.CONNECTION_ERROR, "Sin conexion con https://www.dubvoice.ai/api/v1/video: ReadTimeout: The read operation timed out",
              ambiguous=True)


def start_frame_from_video(pid, si=0, video=None):
    """El start frame de la escena = primer fotograma del video simulado (asi la huella visual de un video recuperado coincide)."""
    f = T.TMP / "fp.mp4"
    f.write_bytes(video or T.make_video(8))
    img = store.get(pid)["scenes"][si]["image"]["file"]
    media.extract_frame(f, 0.1, store.pdir(pid) / img, width=270)


def cand_for(pid, *, dt=2.0, cid="cand-1", **kw):
    a = T.f5_of(pid)["attempts"][-1]
    return {"id": cid, "model": "veo-3.1-fast", "created_at": a["created_at"] + dt, "status": "completed", "duration": 8.0, "result_url": None, **kw}


def install_lister(monkeypatch, fn):
    monkeypatch.setattr(dubvoice, "list_generations", fn)


def ambiguous_clip(monkeypatch, scenes=1, **fake_kw):
    """Un clip cuyo POST quedo ambiguo (ReadTimeout). Devuelve (pid, dub, goog)."""
    dub, goog = T.install(monkeypatch, T.Fake(script=[T.fail_before_submit(AMB)]))
    pid = T.make_project(scenes=scenes)
    start_frame_from_video(pid, 0)
    return pid, dub, goog


# ================================================================== 1. POST ReadTimeout tras crear el job (servidor local real)
class FakeDubVoice:
    """Servidor local: el POST crea el job pero responde DESPUES del timeout del cliente; GET de estado y de archivo funcionan."""

    def __init__(self, post_delay: float):
        self.post_delay, self.posts, self.jobs = post_delay, 0, {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.posts += 1
                jid = f"srv-job-{outer.posts}"
                outer.jobs[jid] = {"created": time.time()}
                time.sleep(outer.post_delay)
                try:
                    self._json({"task_id": jid, "status": "processing"})
                except Exception:
                    pass

            def do_GET(self):
                if self.path.startswith("/files/"):
                    b = T.make_video(8)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                jid = self.path.split("=")[-1]
                if jid in outer.jobs:
                    self._json({"task_id": jid, "status": "completed", "video_url": f"http://127.0.0.1:{outer.port}/files/{jid}.mp4"})
                else:
                    self._json({"error": "not found"}, 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_1_post_readtimeout_after_remote_creation_is_reconciled_with_exactly_one_post(monkeypatch):
    srv = FakeDubVoice(post_delay=2.5)
    try:
        monkeypatch.setattr(dubvoice, "BASE", f"http://127.0.0.1:{srv.port}")
        monkeypatch.setenv("DUBVOICE_API_KEY", "k-test")
        vj._overrides.update(submission_timeout=1.0)
        monkeypatch.setattr(google_veo, "veo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Google no debe usarse")))
        pid = T.make_project()
        start_frame_from_video(pid, 0)

        def lister(**k):                                   # la reconciliacion ve el job que el servidor YA creo (marca de tiempo real)
            a = T.f5_of(pid)["attempts"][-1]
            return [{"id": j, "model": "veo-3.1-fast", "created_at": v["created"], "status": "completed", "duration": 8.0, "result_url": None}
                    for j, v in srv.jobs.items() if abs(v["created"] - a["created_at"]) < 30]

        install_lister(monkeypatch, lister)
        res, logs = T.run(pid)
        f5 = T.f5_of(pid)
        a = f5["attempts"][0]
        assert srv.posts == 1                                                        # UN solo POST en toda la corrida
        assert a["job_id"] == "srv-job-1" and a["reconciled"] and a["status"] == "VALIDATED" and a["adoption_evidence"]["similarity"] > 0.9
        assert f5["state"] == "ACCEPTED" and (store.pdir(pid) / a["raw"]).exists() and T.clips_of(pid)[0]["status"] == "done"
        assert any("RECONCILING" in x for x in logs) and any("inequivoca" in x for x in logs)
        assert len(f5["attempts"]) == 1 and vj.paid_attempts(f5) == 1
    finally:
        srv.close()


# ================================================================== 2-6. reconciliacion (proveedor simulado)
def test_2_reconciliation_finds_the_job_unambiguously_keeps_raw_and_accepts(monkeypatch):
    pid, dub, goog = ambiguous_clip(monkeypatch)
    install_lister(monkeypatch, lambda **k: [cand_for(pid)])
    res, logs = T.run(pid)
    f5 = T.f5_of(pid)
    a = f5["attempts"][0]
    assert len(dub.calls) == 1 and dub.resumes == ["cand-1"] and not goog.calls        # 1 POST; luego solo se consulta ESE job
    assert a["job_id"] == "cand-1" and a["raw"] and (store.pdir(pid) / a["raw"]).exists() and a["reconcile_evidence"]["reasons"]
    assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK" and not f5["remote_unknown"] and f5["possible_duplicate"]


def test_3_reconciliation_with_zero_candidates_goes_to_review_with_zero_more_posts(monkeypatch):
    pid, dub, goog = ambiguous_clip(monkeypatch)
    install_lister(monkeypatch, lambda **k: [])
    res, _ = T.run(pid)
    f5 = T.f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "AMBIGUOUS_SUBMIT" and f5["attempts"][0]["reconcile_result"] == "NONE"
    assert len(dub.calls) == 1 and not dub.resumes and not goog.calls and f5["possible_duplicate"] and f5["attempts"][0]["status"] == "AMBIGUOUS"


def test_4_reconciliation_with_multiple_candidates_never_picks_one(monkeypatch):
    pid, dub, goog = ambiguous_clip(monkeypatch)
    install_lister(monkeypatch, lambda **k: [cand_for(pid, cid="c-a"), cand_for(pid, cid="c-b", dt=5.0)])
    res, _ = T.run(pid)
    f5 = T.f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["attempts"][0]["reconcile_result"] == "MULTIPLE" and not f5["attempts"][0].get("job_id")
    assert len(dub.calls) == 1 and not dub.resumes and "VARIOS" in f5["review_message"]


def test_5_restart_during_ambiguous_or_reconciling_makes_no_post_and_retries_safe_recovery(monkeypatch):
    dub, goog = T.install(monkeypatch)
    pid = T.make_project()
    start_frame_from_video(pid, 0)
    att = vj.new_attempt(pid, 0, 0, provider="dubvoice", model="veo-3.1-fast", prompt="prompt s0c0", asked=8, credits=7500)
    vj.patch_attempt(pid, 0, 0, att["id"], status="RECONCILING")                      # la app murio A MITAD de la reconciliacion
    vj.transition(pid, 0, 0, vj.SUBMITTED)
    T._restart()
    f5 = T.f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "AMBIGUOUS_SUBMIT" and f5["possible_duplicate"]
    asked = []
    install_lister(monkeypatch, lambda **k: asked.append(1) or [])                      # recuperacion segura: solo LECTURAS; no encuentra nada
    T.run(pid)
    assert asked and dub.calls == [] and T.f5_of(pid)["state"] == "NEEDS_REVIEW"
    install_lister(monkeypatch, lambda **k: [cand_for(pid, cid="late-job")])            # ahora si aparece: se recupera SIN POST
    T.run(pid)
    f5 = T.f5_of(pid)
    assert dub.calls == [] and dub.resumes == ["late-job"] and f5["state"] == "ACCEPTED"


def test_6_reconciliation_timeout_or_listing_errors_go_to_review_without_post(monkeypatch):
    pid, dub, _ = ambiguous_clip(monkeypatch)
    vj._overrides.update(reconcile_timeout=0.3, reconcile_interval=0.1)
    n = {"i": 0}
    install_lister(monkeypatch, lambda **k: n.__setitem__("i", n["i"] + 1) or [])
    t0 = time.time()
    T.run(pid)
    assert time.time() - t0 < 10 and n["i"] >= 2 and T.f5_of(pid)["state"] == "NEEDS_REVIEW" and len(dub.calls) == 1
    pid2, dub2, _ = ambiguous_clip(monkeypatch)
    install_lister(monkeypatch, lambda **k: (_ for _ in ()).throw(RuntimeError("503 listando")))
    T.run(pid2)
    assert T.f5_of(pid2)["state"] == "NEEDS_REVIEW" and len(dub2.calls) == 1


def test_default_dubvoice_has_no_documented_listing_so_reconciliation_is_unsupported_and_safe(monkeypatch):
    with pytest.raises(NotSupported):
        dubvoice.list_generations(since=0, until=1)
    pid, dub, goog = ambiguous_clip(monkeypatch)               # sin parchear el lister: NotSupported real
    res, _ = T.run(pid)
    f5 = T.f5_of(pid)
    assert f5["state"] == "NEEDS_REVIEW" and f5["attempts"][0]["reconcile_result"] == "UNSUPPORTED" and "adopt" in f5["review_message"]
    assert len(dub.calls) == 1 and not goog.calls


# ================================================================== matching conservador (funcion pura)
def test_match_candidates_policy_is_conservative():
    cfg = vj.config()
    att = {"id": "a1", "created_at": 1000.0, "model": "veo-3.1-fast", "provider": "dubvoice", "prompt": "Mismo plano medio fijo, a la altura de los ojos " * 3}
    base = {"id": "x1", "model": "veo-3.1-fast", "created_at": 1005.0}
    assert vj.match_candidates(att, [base], set(), [], cfg)[0] == "MATCH"
    assert vj.match_candidates(att, [], set(), [], cfg)[0] == "NONE"
    assert vj.match_candidates(att, [{**base, "model": "veo-3.1"}], set(), [], cfg)[0] == "NONE"                # otro modelo
    assert vj.match_candidates(att, [{**base, "created_at": 1000 + 4000}], set(), [], cfg)[0] == "NONE"          # fuera de ventana
    assert vj.match_candidates(att, [{**base, "created_at": None}], set(), [], cfg)[0] == "NONE"                 # solo la hora NO basta: sin hora no hay match
    assert vj.match_candidates(att, [base], {"x1"}, [], cfg)[0] == "NONE"                                        # ya pertenece a otro intento
    assert vj.match_candidates(att, [{**base, "prompt": "otro prompt totalmente distinto"}], set(), [], cfg)[0] == "NONE"
    assert vj.match_candidates(att, [{**base, "prompt": att["prompt"][:70]}], set(), [], cfg)[0] == "MATCH"      # prefijo >= 60
    assert vj.match_candidates(att, [base, {**base, "id": "x2", "created_at": 1009}], set(), [], cfg)[0] == "MULTIPLE"
    other = {"provider": "dubvoice", "model": "veo-3.1-fast", "created_at": 1003.0}
    assert vj.match_candidates(att, [base], set(), [other], cfg)[0] == "MULTIPLE"                                # no se distingue de otro envio nuestro
    assert vj.match_candidates(att, [{**base, "prompt": att["prompt"]}], set(), [other], cfg)[0] == "MATCH"      # con prompt si se distingue


# ================================================================== 11. NINGUNA ruta de AMBIGUOUS_SUBMIT llega a submit() automaticamente
def _assert_no_submit(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("generate_raw (POST de creacion) invocado desde una ruta AMBIGUOUS")

    monkeypatch.setattr(videos, "generate_raw", boom)
    return calls


def test_11_no_ambiguous_path_can_reach_submit_automatically(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    pid, dub, goog = ambiguous_clip(monkeypatch)
    T.run(pid)                                                                          # deja el clip AMBIGUO (1 POST legitimo)
    assert len(dub.calls) == 1 and T.f5_of(pid)["review_reason"] == "AMBIGUOUS_SUBMIT"
    calls = _assert_no_submit(monkeypatch)
    client = TestClient(app)
    T.run(pid)                                                                          # (a) corrida automatica
    T.run(pid, keys=[(0, 0)])                                                           # (b) corrida con clave, no explicita
    T.run(pid, keys=[(0, 0)], explicit=True)                                            # (c) Regenerar (boton) sin paid
    supervisor.start(pid, lambda msg=None, progress=None: None)                         # (d) supervisor
    T._restart()                                                                        # (e) reinicio de la app
    T.run(pid)
    for url in (f"/api/projects/{pid}/videos/generate", f"/api/projects/{pid}/videos/0/0/regenerate", f"/api/projects/{pid}/supervisor/start"):
        assert client.post(url).status_code in (200, 400, 409)                          # (f) endpoints (incluido el boton Regenerar)
        end = time.time() + 15
        while time.time() < end and vj.SCHED.active:
            time.sleep(0.05)
    assert client.post(f"/api/projects/{pid}/videos/0/0/adopt", json={"result_url": "https://127.0.0.1:1/x.mp4"}).status_code == 200   # (g) /adopt
    end = time.time() + 30
    while time.time() < end and client.get(f"/api/projects/{pid}").json()["jobs"].get("adopt:0:0", {}).get("status") == "running":
        time.sleep(0.1)
    assert calls == [] and len(dub.calls) == 1 and not goog.calls
    f5 = T.f5_of(pid)
    assert f5["round"] == 1 and f5["possible_duplicate"] and f5["state"] in ("NEEDS_REVIEW", "ACCEPTED")          # /adopt recupero (fake) o fallo: NUNCA con un POST


def test_11b_the_single_door_to_submit_refuses_while_an_ambiguous_attempt_is_unresolved(monkeypatch):
    pid, dub, _ = ambiguous_clip(monkeypatch)
    T.run(pid)
    run_ = vj.Run(pid, None, None, None, False)
    d = vj.ClipDriver(run_, 0, 0)
    calls = _assert_no_submit(monkeypatch)
    with pytest.raises(vj._Stop) as e:
        d._attempt(vj.Plan("dubvoice", "veo-3.1-fast", "p", 8, 7500, False, "first"))
    assert e.value.reason == "AMBIGUOUS_SUBMIT" and calls == []
    f5 = vj._blank_f5()
    assert vj.decide(f5, AMB, vj.Ctx(vj.config(), "dubvoice", True, "google", 7500)).kind == "review"          # ni el tipo de decision es 'retry'
    assert vj.config().ambiguous_submit_retries == 0


def test_11c_only_an_explicit_paid_decision_opens_a_new_paid_round(monkeypatch):
    pid, dub, _ = ambiguous_clip(monkeypatch)
    T.run(pid)
    T.run(pid, keys=[(0, 0)], explicit=True, paid=True)
    assert T.f5_of(pid)["round"] == 2 and T.f5_of(pid)["state"] == "ACCEPTED" and len(dub.calls) == 2


# ================================================================== adopcion manual (recuperar el canario huerfano sin pagar)
def _serve_video(video_bytes):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(video_bytes)))
            self.end_headers()
            self.wfile.write(video_bytes)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _adopt_env(monkeypatch, video_bytes):
    srv = _serve_video(video_bytes)
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v.mp4"          # loopback: adopt_remote lo admite solo para pruebas


def test_adopt_recovers_the_orphan_video_without_any_post(monkeypatch):
    pid, dub, goog = ambiguous_clip(monkeypatch)
    T.run(pid)
    assert T.f5_of(pid)["review_reason"] == "AMBIGUOUS_SUBMIT"
    srv, url = _adopt_env(monkeypatch, T.make_video(8))
    try:
        vj.adopt_remote(pid, 0, 0, result_url=url)
    finally:
        srv.shutdown()
    f5 = T.f5_of(pid)
    a = f5["attempts"][0]
    assert f5["state"] == "ACCEPTED" and a["adopted"] and a["adoption_evidence"]["similarity"] > 0.9 and a["adoption_evidence"]["duration"] >= 7.5
    assert len(dub.calls) == 1 and not goog.calls and (store.pdir(pid) / a["raw"]).exists() and a["job_id"].startswith("adopted-")


def test_adopt_rejects_a_video_that_is_not_this_clips_and_keeps_it_aside(monkeypatch):
    pid, dub, _ = ambiguous_clip(monkeypatch, scenes=2)
    T.run(pid, keys=[(0, 0)])
    # el video remoto NO se parece al start frame de la escena 1 (se parece al de la escena 2)
    other = T.TMP / "other.mp4"
    media.run(["-f", "lavfi", "-i", "color=c=green:s=360x640:d=8:r=24", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "8", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-c:a", "aac", str(other)])
    img1 = store.get(pid)["scenes"][1]["image"]["file"]
    media.extract_frame(other, 0.1, store.pdir(pid) / img1, width=270)
    srv, url = _adopt_env(monkeypatch, other.read_bytes())
    try:
        vj.adopt_remote(pid, 0, 0, result_url=url)
    finally:
        srv.shutdown()
    f5 = T.f5_of(pid, 0)
    a = f5["attempts"][0]
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "ADOPTION_REJECTED" and a["status"] == "ADOPTION_REJECTED"
    assert a["bad_raw"] and (store.pdir(pid) / a["bad_raw"]).exists() and not a.get("raw") and "parecido" in " ".join(a["adoption_evidence"]["failed"])
    assert len(dub.calls) == 1 and T.clips_of(pid)[0]["status"] == "error"


def test_adopt_refuses_wrong_state_and_reused_handles(monkeypatch):
    dub, _ = T.install(monkeypatch)
    pid = T.make_project(scenes=2)
    T.run(pid)
    with pytest.raises(ValueError):                                                     # un clip ACCEPTED no se "adopta"
        vj.adopt_remote(pid, 0, 0, result_url="https://x.example/v.mp4")
    with pytest.raises(ValueError):
        vj.adopt_remote(pid, 0, 0)                                                      # sin handle
    with pytest.raises(ValueError):
        vj.adopt_remote(pid, 0, 0, result_url="http://x.example/v.mp4")                 # http plano
    pid2, dub2, _ = ambiguous_clip(monkeypatch)
    T.run(pid2)
    with store.edit(pid2) as q:                                                          # otra escena/clip ya usa ese handle
        q["scenes"].append({"idx": 1, "image": q["scenes"][0]["image"], "clips": [{"idx": 0, "status": "done", "f5": {"attempts": [
            {"id": "a1", "job_id": "job-used", "result_url": "https://x.example/used.mp4"}], "current_attempt": "a1"}}]})
    for kw in ({"job_id": "job-used"}, {"result_url": "https://x.example/used.mp4?sig=1"}):
        with pytest.raises(ValueError):
            vj.adopt_remote(pid2, 0, 0, **kw)
    assert T.f5_of(pid2)["state"] == "NEEDS_REVIEW" and len(dub2.calls) == 1


# ================================================================== 7. telemetria: SUBMITTING visible
def test_7_submitting_is_visible_in_progress_and_summary(monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def hang_before_response(ctx):                     # el POST esta en vuelo: aun no hay job_id
        entered.set()
        release.wait(20)
        raise AMB

    dub, _ = T.install(monkeypatch, T.Fake(script=[hang_before_response]))
    pid = T.make_project()
    start_frame_from_video(pid, 0)
    msgs = []
    t = threading.Thread(target=lambda: vj.run_project(pid, prog=lambda m=None, p=None: msgs.append(m), log=lambda m: None), daemon=True)
    t.start()
    assert entered.wait(10)
    end = time.time() + 6
    while time.time() < end and not any("1 enviando" in (m or "") for m in msgs):
        time.sleep(0.1)
    p = store.get(pid)
    assert vj.clip_phase(p["scenes"][0]["clips"][0]) == "SUBMITTING" and vj.summarize(p)["by_phase"] == {"SUBMITTING": 1}
    assert any("1 enviando" in (m or "") for m in msgs) and not any(("1 generando" in (m or "")) for m in msgs)       # antes: "0 generando" a secas
    release.set()
    t.join(30)
    assert T.f5_of(pid)["state"] == "NEEDS_REVIEW"


# ================================================================== 8 / 10. creditos, legacy, duracion, fallback
def test_credit_semantics_distinguish_submitted_confirmed_and_ambiguous(monkeypatch):
    pid, dub, _ = ambiguous_clip(monkeypatch, scenes=2)
    f = store.path(pid, "videos", "old.mp4")
    f.write_bytes(T.make_video(8))
    with store.edit(pid) as q:
        q["scenes"][1]["clips"][0].update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), task_id="OLD", verified=True)
    with vj.clip_edit(pid, 1, 0):
        pass
    T.run(pid, keys=[(0, 0), (1, 0)])
    s = vj.summarize(store.get(pid))
    assert s["submitted_paid_attempts"] == 1 and s["paid_attempts"] == 1 and s["confirmed_remote_jobs"] == 0      # el legacy NO cuenta
    assert s["ambiguous_possible_charges"] == 1 and s["ambiguous_possible_credits"] == 7500 and s["credits_estimated"] == 7500
    assert s["confirmed_charges"] is None and "sin evidencia" in s["confirmed_charges_note"] and "?" not in s["per_provider"]


def test_balance_before_and_after_is_recorded_only_as_an_observed_delta(monkeypatch):
    vj._overrides["track_balance"] = True
    seq = iter([100000, 92500])
    monkeypatch.setattr(dubvoice, "balance", lambda **k: next(seq))
    dub, _ = T.install(monkeypatch)
    pid = T.make_project()
    res, _ = T.run(pid)
    assert res["credits_balance_before"] == 100000 and res["credits_balance_after"] == 92500 and res["credits_balance_delta"] == 7500
    assert res["confirmed_charges"] is None                                             # un delta de saldo NO es un cobro confirmado por clip


def test_9_provider_8s_with_target_2_79_is_valid_and_target_is_never_inflated(monkeypatch):
    dub, goog = T.install(monkeypatch)
    pid = T.make_project(start=3.71, dur=2.79, target=4.4)                              # timeline real del canario #2: 3.71 → 6.50
    with store.edit(pid) as q:
        q["scenes"][0]["clips"][0].update(t_start=3.9, t_end=6.3)
    res, _ = T.run(pid)
    f5 = T.f5_of(pid)
    a = f5["attempts"][0]
    assert f5["source_start"] == 3.71 and f5["source_end"] == 6.5 and f5["target_duration"] == 2.79 and a["target_duration"] == 2.79
    assert 7.5 <= a["provider_duration"] <= 8.5 and f5["state"] == "ACCEPTED" and len(f5["attempts"]) == 1 and T.clips_of(pid)[0]["warning"] is None
    assert f5["target_duration"] != a["provider_duration"]


def test_10_ambiguous_submit_never_falls_back_to_google_or_a_cheaper_model(monkeypatch):
    dub, goog = T.install(monkeypatch, T.Fake(script=[T.fail_before_submit(AMB)]), T.Fake("google"))
    pid = T.make_project(scenes=2)
    T.run(pid)
    assert not goog.calls and {c["model"] for c in dub.calls} == {"veo-3.1-fast"}
    assert T.f5_of(pid, 0)["state"] in ("NEEDS_REVIEW", "ACCEPTED") and not any(c.get("provider_used") == "still" for c in T.clips_of(pid))


# ================================================================== timeouts separados y POST sincrono
def test_timeouts_are_separate_and_the_post_read_timeout_is_not_the_old_hardcoded_60s(monkeypatch, tmp_path):
    vj._overrides.clear()
    cfg = vj.config()
    lim = cfg.limits()
    assert (lim["connect"], lim["submission"], lim["poll_request"], lim["processing"], lim["download"]) == (10, 300, 20, 720, 180)
    assert cfg.reconcile_timeout == 180 and cfg.max_project_time == 7200 and cfg.max_total_generation_time == 1200
    assert len({lim["connect"], lim["submission"], lim["poll_request"], lim["processing"], lim["download"]}) == 5      # no es un unico valor
    net = T.Net()
    T.net_install(monkeypatch, net)
    T.strict_veo(tmp_path, limits={"connect": 7, "submission": 123})
    post = [r for r in net.reqs if r[0] == "POST"][0][2]
    assert post["connect"] == 7 and post["read"] == 123 and post["deadline"] == 123                                        # antes: read=min(60, ...)


def test_sync_post_persists_the_result_handle_before_download_and_recovers_without_a_second_post(monkeypatch, tmp_path):
    err = F5Error(ErrorType.DOWNLOAD_ERROR, "Descarga respondio 503")
    state = {"n": 0}

    def dl(url):
        state["n"] += 1
        return err if state["n"] <= 2 else b"MP4BYTES"

    net = T.Net(post=lambda body: T.R(200, {"file_url": "https://cdn.example/videos/v1.mp4?X-Amz-Signature=SECRET", "status": "completed"}), download=dl)
    T.net_install(monkeypatch, net)
    monkeypatch.setattr(dubvoice, "contract_file", lambda: tmp_path / "contracts" / "dubvoice.json")
    got = []
    rec = dubvoice.ContractRecorder(T.make_project(), "dubvoice", 0, 0, "a1")
    with pytest.raises(F5Error) as e:
        T.strict_veo(tmp_path, limits={"download_retries": 2}, on_submit=lambda j, m=None: got.append((j, m)), recorder=rec)
    assert got and got[0][0].startswith("sync-") and got[0][1]["result_url"].startswith("https://cdn.example/videos/v1.mp4")        # handle ANTES de descargar
    assert e.value.etype == ErrorType.DOWNLOAD_ERROR and e.value.job_id == got[0][0] and len([r for r in net.reqs if r[0] == "POST"]) == 1
    tid, data = dubvoice.resume(got[0][0], result_url=got[0][1]["result_url"], limits=T.LIM)                                       # reintento SOLO de descarga
    assert data == b"MP4BYTES" and len([r for r in net.reqs if r[0] == "POST"]) == 1
    with pytest.raises(F5Error):
        dubvoice.resume(got[0][0], limits=T.LIM)                                                                                   # sin URL no se inventa un sondeo
    c = json.loads((tmp_path / "contracts" / "dubvoice.json").read_text())
    assert c["verified"] is False and c["observed"]["mode"] == "sync_post" and "SECRET" not in json.dumps(c)                        # observado, NO verificado


def test_contract_stays_unverified_without_a_full_identification_and_poll_cycle(monkeypatch, tmp_path):
    net = T.Net(post=lambda body: T.R(200, {"file_url": "https://cdn.example/v.mp4"}))
    T.net_install(monkeypatch, net)
    monkeypatch.setattr(dubvoice, "contract_file", lambda: tmp_path / "contracts" / "dubvoice.json")
    T.strict_veo(tmp_path, recorder=dubvoice.ContractRecorder(T.make_project(), "dubvoice", 0, 0, "a1"))
    assert not dubvoice.contract_verified()
    assert json.loads((tmp_path / "contracts" / "dubvoice.json").read_text())["observed"]["mode"] == "sync_post"


def test_max_project_time_stops_the_run_but_keeps_remote_jobs(monkeypatch):
    vj._overrides["max_project_time"] = 1
    release = threading.Event()
    dub, _ = T.install(monkeypatch, T.Fake(script=[T.hang_after_submit(release)]))
    pid = T.make_project()
    t0 = time.time()
    res, logs = T.run(pid)
    release.set()
    f5 = T.f5_of(pid)
    assert time.time() - t0 < 15 and any("Tope" in x for x in logs)
    assert f5["state"] in ("SUBMITTED", "PROCESSING") and f5["needs_reconcile"] and f5["attempts"][0]["job_id"] and len(dub.calls) == 1


def test_the_suite_itself_cannot_reach_a_real_provider():
    """Evidencia de 0 llamadas reales: conftest bloquea cualquier conexion que no sea loopback."""
    import socket
    s = socket.socket()
    with pytest.raises(RuntimeError, match="RED REAL BLOQUEADA"):
        s.connect(("93.184.216.34", 443))
    s.close()

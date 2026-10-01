"""PASO 3.2.2 · semantica correcta de tiempos (t_gen falso), ContractRecorder para el canario real y modo canario de 1 POST.

Sin red real (conftest bloquea cualquier conexion no loopback): proveedores simulados, HTTP simulado (T.Net) y servidores locales en 127.0.0.1.
"""
from __future__ import annotations

import importlib.util
import json
import re
import time
from pathlib import Path

import pytest

import test_f5_adopt_local as AL
import test_f5_jobs as T
import test_f5_reconcile as R
from app import store
from app.phases import video_jobs as vj, videos
from app.services import dubvoice, google_veo
from app.services.errors import ErrorType, F5Error

env = T.env
SECRET_TOKEN = "TOKENSECRET9f8e7d6c"
SECRET_SIG = "SIGNATUREsecret123456"
SIGNED = f"https://cdn.example/storage/v1/object/public/videos/v1.mp4?token={SECRET_TOKEN}&X-Amz-Signature={SECRET_SIG}&Expires=99999"


def tool(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def project_text(pid, extra=""):
    """TODO lo legible que la app deja para diagnostico: JSONL/JSON del proyecto + el texto que se le pase (logs, reportes, respuestas)."""
    blob = extra
    for f in store.pdir(pid).rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl") and f.name != "project.json":
            blob += f.read_text(errors="ignore")
    return blob


def real_flow(monkeypatch, tmp_path, net, scenes=1, **mp):
    """Ejecuta F5 con el adaptador REAL de DubVoice (camino estricto) sobre HTTP simulado."""
    T.net_install(monkeypatch, net)
    monkeypatch.setattr(dubvoice, "contract_file", lambda: tmp_path / "contracts" / "dubvoice.json")
    pid = T.make_project(scenes=scenes, **mp)
    R.start_frame_from_video(pid, 0)
    logs = []
    res = vj.run_project(pid, log=logs.append)
    return pid, res, logs


ASYNC_POST = lambda body: T.R(200, {"data": {"task_id": "T-9", "status": "queued", "model": "veo-3.1-fast", "duration": 8,   # noqa: E731
                                           "video": {"url": SIGNED}}, "api_key": "echo-should-be-redacted"},
                              {"Set-Cookie": "session=COOKIESECRET", "X-Api-Key": "HDRSECRET", "Authorization": "Bearer HDRBEARER", "X-RateLimit-Remaining": "9"})


# ================================================================== 1-2. semantica de tiempos
def test_1_job_adopted_two_hours_later_has_no_generation_seconds_only_recovery_delay(monkeypatch, tmp_path):
    pid, dub, _ = AL.build_orphan(monkeypatch)
    with store.edit(pid) as q:                                                 # el POST original fue hace 2 h
        a = q["scenes"][0]["clips"][1]["f5"]["attempts"][1]
        a["created_at"] -= 7200
        a["submit_started_at"] -= 7200
    before = json.dumps(T.f5_of(pid, 0, 1), sort_keys=True)
    vj.adopt_local(pid, 0, 1, file=AL.local_video(tmp_path))
    a = T.f5_of(pid, 0, 1)["attempts"][1]
    tm = vj.attempt_timing(a)
    assert tm["generation_seconds"] is None and tm["submit_ack_seconds"] is None
    assert 7200 <= tm["recovery_delay_seconds"] < 7260 and "se desconoce" in tm["timing_note"] and tm["adopted_at"]
    s = vj.summarize(store.get(pid))
    assert s["avg_generation_seconds"] is None and s["max_generation_seconds"] is None and 7200 <= s["max_recovery_delay_seconds"] < 7260
    row = [x for c in s["clips"] for x in c["attempts"] if x["id"] == "a2"][0]
    assert row["timing"]["generation_seconds"] is None and row["timing"]["recovery_delay_seconds"] > 7000
    text = tool("f5_report").build(pid, {(1, 2)}, 0)
    line = [l for l in text.splitlines() if " a2 " in l][0]
    assert re.search(r"\s-\s+7[12]\d\ds\s", line), line                       # t_gen = '-'  y  recup ≈ 7200s
    assert "NO es tiempo de generacion" in text
    # el historial original no se modifico por calcular/mostrar tiempos
    assert json.dumps(T.f5_of(pid, 0, 1), sort_keys=True).count('"created_at"') == before.count('"created_at"')


def test_1b_old_real_shaped_adopted_attempt_never_reports_7094s_as_generation():
    """Forma EXACTA del intento persistido por 1bf65ea (sin los campos nuevos)."""
    t0 = 1_790_000_000.0
    a = {"id": "a2", "provider": "dubvoice", "model": "veo-3.1-fast", "job_id": "local-5b36975a7d9d609a", "status": "VALIDATED", "paid": True, "credits": 7500,
         "created_at": t0, "submitted_at": t0, "completed_at": t0 + 7094, "adopted": True, "adopted_at": t0 + 7090, "provider_duration": 8.0,
         "resolved_ambiguity": {"from_status": "AMBIGUOUS", "by": "local_file"}}
    tm = vj.attempt_timing(a)
    assert tm["generation_seconds"] is None and tm["recovery_delay_seconds"] == 7090.0
    assert vj.attempt_timing({"id": "legacy", "provider": None, "status": "VALIDATED", "legacy": True})["generation_seconds"] is None


def test_2_normal_cycle_reports_the_observed_generation_time(monkeypatch):
    dub, _ = T.install(monkeypatch, T.Fake(delay=0.5))
    pid = T.make_project()
    T.run(pid)
    a = T.f5_of(pid)["attempts"][0]
    tm = vj.attempt_timing(a)
    assert a["submit_started_at"] <= a["submit_response_at"] <= a["download_completed_at"] <= a["completed_at"]
    assert 0.5 <= tm["generation_seconds"] < 6 and tm["recovery_delay_seconds"] is None and tm["submit_ack_seconds"] is not None
    assert abs(tm["generation_seconds"] - (a["download_completed_at"] - a["submit_started_at"])) < 0.01          # NO incluye QC/voz/auditoria
    s = vj.summarize(store.get(pid))
    assert 0.5 <= s["avg_generation_seconds"] < 6 and s["recovery_delays_seconds"] == [] and s["max_recovery_delay_seconds"] is None


def test_sync_post_generation_time_ends_at_the_response(monkeypatch, tmp_path):
    net = T.Net(post=lambda body: (time.sleep(0.4), T.R(200, {"file_url": SIGNED, "status": "completed"}))[1], download=lambda url: T.make_video(8))
    pid, res, logs = real_flow(monkeypatch, tmp_path, net)
    a = T.f5_of(pid)["attempts"][0]
    tm = vj.attempt_timing(a)
    assert a["remote_completed_at"] and 0.4 <= tm["generation_seconds"] < 5 and tm["recovery_delay_seconds"] is None


# ================================================================== 3-5. ContractRecorder: sanitizacion y esquema
def test_3_recorder_never_stores_authorization_api_keys_cookies_or_sensitive_headers(monkeypatch, tmp_path):
    net = T.Net(post=ASYNC_POST, polls=[T.R(200, {"status": "completed", "video_url": SIGNED})], download=lambda url: T.make_video(8))
    pid, res, logs = real_flow(monkeypatch, tmp_path, net)
    sent = [r[2].get("headers", {}) for r in net.reqs if r[0] == "POST"][0]
    assert "Authorization" in sent and T.CANARY in sent["Authorization"]                                         # la peticion SI lleva la clave (en memoria)...
    blob = project_text(pid, "".join(logs) + json.dumps(res, default=str)) + (tmp_path / "contracts" / "dubvoice.json").read_text()
    for secret in (T.CANARY, "COOKIESECRET", "HDRSECRET", "HDRBEARER", "echo-should-be-redacted", "Bearer "):
        assert secret not in blob, secret                                                                       # ...pero NUNCA queda en logs/contrato/estado diagnostico
    first = [json.loads(l) for l in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines() if '"op": "submit"' in l][0]
    assert "set-cookie" in first["header_names"] and "authorization" in first["header_names"] and "set-cookie" not in first["headers"]
    assert first["headers"].get("X-RateLimit-Remaining") == "9" and first["body"]["api_key"] == "<redacted>"


def test_4_signed_url_token_never_reaches_logs_reports_endpoints_or_contract(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    net = T.Net(post=ASYNC_POST, polls=[T.R(200, {"status": "completed", "video_url": SIGNED})], download=lambda url: T.make_video(8))
    pid, res, logs = real_flow(monkeypatch, tmp_path, net)
    client = TestClient(app)
    api = client.get(f"/api/projects/{pid}/f5/summary").text + client.get(f"/api/projects/{pid}/f5/clip/0/0").text
    report = tool("f5_report").build(pid, None, 50)
    blob = project_text(pid, "".join(logs) + json.dumps(res, default=str) + api + report + (tmp_path / "contracts" / "dubvoice.json").read_text())
    assert SECRET_TOKEN not in blob and SECRET_SIG not in blob and "Expires=99999" not in blob
    ex = [json.loads(l) for l in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines() if '"op": "submit"' in l][0]["extracted"]
    assert ex["urls"][0]["host"] == "cdn.example" and ex["urls"][0]["had_query"] and "?" not in ex["urls"][0]["redacted"]
    # el estado PRIVADO conserva la URL completa solo donde hace falta (project.json, campo result_url del intento) y nunca la expone
    pj = (store.pdir(pid) / "project.json").read_text()
    a = T.f5_of(pid)["attempts"][0]
    assert a.get("result_url_redacted", "").find(SECRET_TOKEN) < 0 and "result_url" not in client.get(f"/api/projects/{pid}/f5/clip/0/0").json()["attempts"][0]
    assert pj.count(SECRET_TOKEN) <= 1


def test_5_json_response_captures_schema_keys_ids_status_model_duration_and_timing(monkeypatch, tmp_path):
    net = T.Net(post=ASYNC_POST, polls=[T.R(200, {"status": "completed", "video_url": SIGNED})], download=lambda url: T.make_video(8))
    pid, res, logs = real_flow(monkeypatch, tmp_path, net)
    recs = [json.loads(l) for l in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines()]
    sub = [r for r in recs if r["op"] == "submit"][0]
    assert sub["http_status"] == 200 and sub["content_type"] == "application/json" and sub["elapsed_seconds"] >= 0 and sub["content_length"] > 10
    assert sub["url"].endswith("/api/v1/video") and sub["method"] == "POST" and sub["request_shape"]["model"] == "veo-3.1-fast"
    ex = sub["extracted"]
    assert {"data", "data.task_id", "data.status", "data.model", "data.duration", "data.video", "data.video.url"} <= set(ex["json_keys"])
    assert ex["ids"] == {"data.task_id": "T-9"} and ex["status"] == {"path": "data.status", "value": "queued"} and ex["model"]["value"] == "veo-3.1-fast"
    assert ex["durations"] == {"data.duration": 8} and sub["json_shape"]["data"]["task_id"].startswith("str(")
    poll = [r for r in recs if r["op"] == "poll"][0]
    assert poll["url"].endswith("/api/v1/video") and poll["extracted"]["status"]["value"] == "completed" and poll["extracted"]["urls"][0]["host"] == "cdn.example"
    c = json.loads((tmp_path / "contracts" / "dubvoice.json").read_text())
    assert c["verified"] and c["submit_id_field"] == "data.task_id" and c["poll_endpoint"] == "/api/v1/video"
    ops = [r["op"] for r in recs]
    assert ops.index("submit") < ops.index("poll") < ops.index("download")


# ================================================================== 6-7. POST sincrono y persistencia del id
def test_6_sync_response_with_result_url_continues_to_download_and_accepts(monkeypatch, tmp_path):
    net = T.Net(post=lambda body: T.R(200, {"file_url": SIGNED, "status": "completed"}), download=lambda url: T.make_video(8))
    pid, res, logs = real_flow(monkeypatch, tmp_path, net)
    f5 = T.f5_of(pid)
    a = f5["attempts"][0]
    assert f5["state"] == "ACCEPTED" and a["job_id"].startswith("sync-") and a["result_url"].startswith("https://cdn.example/") and a["raw"]
    assert len([r for r in net.reqs if r[0] == "POST"]) == 1 and not [r for r in net.reqs if r[0] == "GET"]       # sin sondeo: la respuesta ya traia el resultado
    assert not dubvoice.contract_verified()                                                                       # observado (sync_post), NO verificado


def test_7_task_id_is_persisted_before_the_first_poll(monkeypatch, tmp_path):
    seen = []

    class N(T.Net):
        def request_once(self, method, url, **kw):
            if method == "GET":
                seen.append(T.f5_of(PID[0])["attempts"][0].get("job_id"))               # estado en disco EN el momento del primer sondeo
            return super().request_once(method, url, **kw)

    PID = [None]
    net = N(post=ASYNC_POST, polls=[T.R(200, {"status": "processing"}), T.R(200, {"status": "completed", "video_url": SIGNED})], download=lambda u: T.make_video(8))
    T.net_install(monkeypatch, net)
    monkeypatch.setattr(dubvoice, "contract_file", lambda: tmp_path / "contracts" / "dubvoice.json")
    pid = T.make_project()
    PID[0] = pid
    R.start_frame_from_video(pid, 0)
    vj.run_project(pid, log=lambda m: None)
    assert seen and all(j == "T-9" for j in seen) and T.f5_of(pid)["state"] == "ACCEPTED"


# ================================================================== 8-9. timeouts
def test_8_post_read_timeout_is_300s_not_the_old_60s(monkeypatch, tmp_path):
    vj._overrides.clear()
    lim = vj.config().limits()
    assert (lim["connect"], lim["submission"]) == (10, 300)
    net = T.Net()
    T.net_install(monkeypatch, net)
    T.strict_veo(tmp_path, limits=lim)
    post = [r for r in net.reqs if r[0] == "POST"][0][2]
    assert post["connect"] == 10 and post["read"] == 300 and post["deadline"] == 300                    # un POST de 61-299 s NO se corta
    for f in ("app/services/dubvoice.py", "app/services/google_veo.py"):
        src = (Path(__file__).resolve().parents[1] / f).read_text()
        assert "min(60" not in src, f"{f}: timeout de lectura fijado en 60 s"


def test_8b_a_post_slower_than_the_old_hard_limit_but_within_the_configured_one_succeeds(monkeypatch):
    """A escala: submission=3 s y el servidor tarda 1.5 s; la regla antigua min(60, ·) no se distinguiria, asi que se verifica contra el servidor real."""
    srv = R.FakeDubVoice(post_delay=1.5)
    try:
        monkeypatch.setattr(dubvoice, "BASE", f"http://127.0.0.1:{srv.port}")
        monkeypatch.setenv("DUBVOICE_API_KEY", "k-test")
        vj._overrides.update(submission_timeout=3.0)
        pid = T.make_project()
        R.start_frame_from_video(pid, 0)
        R.install_lister(monkeypatch, lambda **k: [])
        vj.run_project(pid, log=lambda m: None)
        f5 = T.f5_of(pid)
        assert srv.posts == 1 and f5["attempts"][0]["job_id"] == "srv-job-1" and f5["state"] == "ACCEPTED"       # sin timeout artificial
    finally:
        srv.close()


def test_9_post_beyond_the_timeout_is_ambiguous_with_exactly_one_post_and_zero_retries(monkeypatch):
    srv = R.FakeDubVoice(post_delay=2.5)
    try:
        monkeypatch.setattr(dubvoice, "BASE", f"http://127.0.0.1:{srv.port}")
        monkeypatch.setenv("DUBVOICE_API_KEY", "k-test")
        vj._overrides.update(submission_timeout=1.0)
        monkeypatch.setattr(google_veo, "veo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Google no debe usarse")))
        pid = T.make_project()
        R.start_frame_from_video(pid, 0)
        res, logs = T.run(pid)
        f5 = T.f5_of(pid)
        assert srv.posts == 1 and f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "AMBIGUOUS_SUBMIT" and f5["attempts"][0]["status"] == "AMBIGUOUS"
        T.run(pid)
        T._restart()
        T.run(pid)
        assert srv.posts == 1 and vj.paid_attempts(T.f5_of(pid)) == 1 and vj.config().ambiguous_submit_retries == 0
        assert any("sin respuesta del POST" in x for x in logs)                                                   # el canario lo dice claramente
    finally:
        srv.close()


# ================================================================== 10-11. audio no regenera; target 2.79
def test_10_audio_failure_in_canary_mode_never_regenerates_video(monkeypatch):
    from app.services import stt
    vj._overrides.update(single_post=True, skip_voice=True, max_attempts_per_clip=1, fallback_provider="none")
    dub, goog = T.install(monkeypatch)
    monkeypatch.setattr(stt, "transcribe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("whisper missing")))
    pid = T.make_project(unify=True)
    voice = []
    monkeypatch.setattr(dubvoice, "voice_change", lambda *a, **k: voice.append(1))
    T.run(pid)
    f5 = T.f5_of(pid)
    assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK" and f5["audio_state"] == "NEEDS_FIX" and not voice          # skip_voice: sin cambio de voz
    assert len(dub.calls) == 1 and not goog.calls and vj.paid_attempts(f5) == 1 and (store.pdir(pid) / f5["attempts"][0]["raw"]).exists()


def test_11_target_2_79_with_provider_8_stays_valid_in_canary_mode(monkeypatch):
    vj._overrides.update(single_post=True, skip_voice=True)
    dub, _ = T.install(monkeypatch)
    pid = T.make_project(start=3.71, dur=2.79, target=4.4)
    res, _ = T.run(pid)
    f5 = T.f5_of(pid)
    a = f5["attempts"][0]
    assert f5["target_duration"] == 2.79 and 7.5 <= a["provider_duration"] <= 8.5 and f5["state"] == "ACCEPTED" and len(f5["attempts"]) == 1


# ================================================================== modo canario: 1 POST, logs legibles, CLI
def test_canary_single_post_never_chains_a_second_post_even_after_a_429_or_connect_error(monkeypatch):
    vj._overrides.update(single_post=True, max_attempts_per_clip=1, fallback_provider="none")
    for exc in (F5Error(ErrorType.RATE_LIMIT, "429", retry_after=0.1, http_status=429), F5Error(ErrorType.CONNECTION_ERROR, "no route", ambiguous=False),
                F5Error(ErrorType.UNKNOWN_ERROR, "boom")):
        vj.SCHED.reset_for_tests()
        dub, goog = T.install(monkeypatch, T.Fake(script=[T.fail_before_submit(exc)] * 4))
        pid = T.make_project()
        T.run(pid)
        f5 = T.f5_of(pid)
        assert len(dub.calls) == 1 and not goog.calls and f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "CANARY_SINGLE_POST", exc.etype


def test_canary_prints_every_stage_in_order(monkeypatch, tmp_path):
    net = T.Net(post=ASYNC_POST, polls=[T.R(200, {"status": "processing"}), T.R(200, {"status": "completed", "video_url": SIGNED})], download=lambda u: T.make_video(8))
    vj._overrides.update(single_post=True, skip_voice=True)
    pid, res, logs = real_flow(monkeypatch, tmp_path, net)
    text = "\n".join(logs)
    stages = ["SUBMITTING", "HTTP 200 recibido en", "contrato capturado", "job ", "RAW descargado y guardado", "provider_duration=8", "target_duration=",
              "QC escena 1 clip 1", "Aceptado"]
    pos = [text.find(s) for s in stages]
    assert all(p >= 0 for p in pos), dict(zip(stages, pos))
    assert pos == sorted(pos), dict(zip(stages, pos))
    assert SECRET_TOKEN not in text and T.CANARY not in text


def _canary_project(monkeypatch, accepted_legacy=True, model="veo-3.1-fast", unify=True):
    dub, goog = T.install(monkeypatch)
    pid = T.make_project(scenes=2, start=3.71, dur=2.79, unify=unify)
    f = store.path(pid, "videos", "legacy.mp4")
    f.write_bytes(T.make_video(8))
    with store.edit(pid) as q:
        q["settings"]["dubvoice_video_model"] = model
        if accepted_legacy:
            for s in q["scenes"]:
                s["clips"][0].update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), task_id="OLD", verified=True)
    R.start_frame_from_video(pid, 0)
    return pid, dub, goog


def test_canary_cli_dry_run_makes_no_calls_no_clone_and_no_changes(monkeypatch, capsys):
    pid, dub, goog = _canary_project(monkeypatch)
    mod = tool("f5_canary")
    before = (store.pdir(pid) / "project.json").read_text()
    n_dirs = len(list(store.pdir(pid).parent.iterdir()))
    rc = mod.main([pid, "--scene", "0", "--clip", "0"])
    out = capsys.readouterr().out
    assert rc == 0 and "SIMULACRO" in out and "0 llamadas, 0 POST, 0 creditos" in out and '"concurrency": 1' in out and '"fallback": "none"' in out
    assert '"ambiguous_retries": 0' in out and '"post_read_timeout": 300' in out and '"single_post": true' in out and '"skip_voice": true' in out
    assert not dub.calls and not goog.calls and (store.pdir(pid) / "project.json").read_text() == before and len(list(store.pdir(pid).parent.iterdir())) == n_dirs
    vj._overrides.clear()


def test_canary_cli_preflight_aborts_on_wrong_model_or_dirty_clip(monkeypatch, capsys):
    mod = tool("f5_canary")
    pid, dub, _ = _canary_project(monkeypatch, model="omniflash")
    assert mod.main([pid, "--scene", "0", "--clip", "0", "--yes-i-accept-one-paid-post"]) == 2 and not dub.calls
    assert "veo-3.1-fast" in capsys.readouterr().out
    pid2, dub2, _ = AL.build_orphan(monkeypatch)                                  # clip con envio ambiguo previo: NO es un clip limpio
    assert mod.main([pid2, "--scene", "0", "--clip", "1", "--yes-i-accept-one-paid-post"]) == 2
    assert "NO es un clip limpio" in capsys.readouterr().out and len(dub2.calls) == 1
    vj._overrides.clear()


def test_canary_cli_real_mode_uses_a_copy_one_post_and_blocks_a_second(monkeypatch, capsys):
    pid, dub, goog = _canary_project(monkeypatch)
    mod = tool("f5_canary")
    saved = (videos.generate_raw, dubvoice.veo, google_veo.veo)
    orig = (store.pdir(pid) / "project.json").read_text()
    try:
        rc = mod.main([pid, "--scene", "0", "--clip", "0", "--yes-i-accept-one-paid-post"])
        out = capsys.readouterr().out
        assert rc == 0 and "CANARY: PASS" in out and "POST de generacion realizados por este proceso: 1" in out and "Copia del proyecto creada" in out
        assert len(dub.calls) == 1 and not goog.calls and (store.pdir(pid) / "project.json").read_text() == orig         # el proyecto real NO se toco
        assert '"target_duration": 2.79' in out and '"raw_exists": true' in out and "SUBMITTING" in out and '"audio_state": "NEEDS_FIX"' in out
        clone = re.search(r"Copia del proyecto creada: (\S+)", out).group(1)
        assert clone.startswith(pid + "-canary-") and (store.pdir(clone) / "f5_events.jsonl").exists() and not (store.pdir(clone) / "videos" / "legacy.mp4").exists()
        assert T.f5_of(clone, 0, 0)["round"] == 2 and vj.paid_attempts(T.f5_of(clone, 0, 0)) == 1
        with pytest.raises(RuntimeError, match="BLOQUEADO"):                                                           # una segunda llamada de generacion es imposible
            dubvoice.veo("p", Path("x.jpg"), limits={})
    finally:
        videos.generate_raw, dubvoice.veo, google_veo.veo = saved
        vj._overrides.clear()

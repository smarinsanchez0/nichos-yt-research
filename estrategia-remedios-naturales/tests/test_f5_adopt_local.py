"""PASO 3.2.1 · adopcion segura de un MP4 LOCAL ya descargado (resuelve un AMBIGUOUS_SUBMIT sin POST ni creditos).

Escenario real reproducido: clip con intento `legacy` + `a2` en AMBIGUOUS_SUBMIT, timeline 3.71 → 6.50 (target 2.79 s), video remoto de 8 s.
Sin red real (conftest bloquea cualquier conexion no loopback) y sin proveedores: cualquier generate_raw/dubvoice.veo/google_veo.veo falla el test.
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path

import pytest

import test_f5_jobs as T
import test_f5_reconcile as R
from app import media, store
from app.phases import video_jobs as vj, videos
from app.services import claude, dubvoice, google_veo

env = T.env
UUID_NAME = "7aafd1ba-2917-4864-b82f-33c24bb1e47a_veo31fast_v1_1790809072605.mp4"
SI, CI = 0, 1


def build_orphan(monkeypatch, unify=False):
    """Escena 0 con 2 clips; el clip 1 estaba 'done' (legacy) y su regeneracion quedo en AMBIGUOUS_SUBMIT (a2)."""
    dub, goog = T.install(monkeypatch, T.Fake(script=[T.fail_before_submit(R.AMB)]))
    pid = T.make_project(scenes=2, clips=1, unify=unify)
    with store.edit(pid) as q:
        s0 = q["scenes"][0]
        s0.update(start=0.0, end=6.5)
        s0["clips"] = [dict(s0["clips"][0], idx=0, t_start=0.2, t_end=3.52, status="done", file=None),
                       dict(s0["clips"][0], idx=1, t_start=3.9, t_end=6.3, status="pending")]
    from PIL import Image
    img1 = store.path(pid, "images", "scene1.jpg")                       # cada escena con SU start frame (make_project comparte un solo archivo)
    Image.new("RGB", (270, 480), (30, 120, 40)).save(img1)
    with store.edit(pid) as q:
        q["scenes"][1]["image"]["file"] = store.rel(pid, img1)
    R.start_frame_from_video(pid, 0)
    f = store.path(pid, "videos", "legacy.mp4")
    f.write_bytes(T.make_video(8))
    with store.edit(pid) as q:
        q["scenes"][0]["clips"][0].update(status="done", file=store.rel(pid, f))
        q["scenes"][0]["clips"][1].update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), provider_used="dubvoice", task_id="OLD", verified=True)
    with store.edit(pid) as q:                                          # la otra escena ya esta lista: este escenario solo tiene UN clip pendiente
        q["scenes"][1]["clips"][0].update(status="done", file=store.rel(pid, f), raw=store.rel(pid, f), provider_used="dubvoice", task_id="OLD2")
    with vj.clip_edit(pid, SI, CI):
        pass
    T.run(pid, keys=[(SI, CI)], explicit=True)                          # el usuario regenera: 1 POST legitimo → ambiguo
    f5 = T.f5_of(pid, SI, CI)
    assert f5["state"] == "NEEDS_REVIEW" and [a["id"] for a in f5["attempts"]] == ["legacy", "a2"] and f5["attempts"][1]["status"] == "AMBIGUOUS"
    assert f5["target_duration"] == 2.79 and len(dub.calls) == 1
    return pid, dub, goog


def local_video(tmp_path, video=None, name=UUID_NAME):
    p = tmp_path / name
    p.write_bytes(video or T.make_video(8))
    return p


def trap_generation(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("se intento GENERAR/ENVIAR un video durante una adopcion local")

    monkeypatch.setattr(videos, "generate_raw", boom)
    return calls


def f5c(pid):
    return T.f5_of(pid, SI, CI)


def clip11(pid):
    return store.get(pid)["scenes"][SI]["clips"][CI]


# ================================================================== adopcion valida
def test_valid_local_adoption_resolves_the_ambiguous_attempt_without_any_submit_or_charge(monkeypatch, tmp_path):
    pid, dub, goog = build_orphan(monkeypatch)
    before = vj.summarize(store.get(pid))
    assert before["ambiguous_possible_charges"] == 1 and before["credits_estimated"] == 7500
    trap = trap_generation(monkeypatch)
    res = vj.adopt_local(pid, SI, CI, file=local_video(tmp_path))
    f5 = f5c(pid)
    a = f5["attempts"][1]
    assert trap == [] and len(dub.calls) == 1 and not dub.resumes and not goog.calls                      # 0 submit; nunca DubVoice ni Google
    assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK" and clip11(pid)["status"] == "done" and clip11(pid)["file"] and clip11(pid)["error"] is None
    # resuelve el intento EXISTENTE: ningun intento ni pago nuevos
    assert [x["id"] for x in f5["attempts"]] == ["legacy", "a2"] and vj.paid_attempts(f5) == 1 and f5["credits_spent"] == 7500 and f5["round"] == 2
    assert a["credits"] == 7500 and a["paid"] and a["status"] == "VALIDATED"
    # historial AMBIGUOUS conservado
    assert a["submit_ambiguous"] and a["possible_duplicate"] and a["error_type"] == "CONNECTION_ERROR"
    assert a["resolved_ambiguity"]["from_status"] == "AMBIGUOUS" and a["resolved_ambiguity"]["by"] == "local_file"
    # el UUID del nombre es solo metadata
    assert a["original_filename"] == UUID_NAME and a["job_id"].startswith("local-") and "7aafd1ba" not in a["job_id"] and a["job_id_kind"] == "local_file"
    # RAW preservado (copia en videos/ + copia en adopt_inbox/) y huella superada
    assert (store.pdir(pid) / a["raw"]).exists() and (store.pdir(pid) / a["local_file"]).exists() and a["raw"] != a["local_file"]
    assert a["adoption_evidence"]["similarity"] >= 0.8 and a["adoption_evidence"]["best_other"] < a["adoption_evidence"]["similarity"] and not a["adoption_evidence"]["failed"]
    # target_duration sigue 2.79 aunque el proveedor entregue ~8 s
    assert f5["target_duration"] == 2.79 and f5["source_start"] == 3.71 and f5["source_end"] == 6.5 and 7.5 <= a["provider_duration"] <= 8.5
    # telemetria: el POST original cuenta UNA vez
    s = vj.summarize(store.get(pid))
    assert s["submitted_paid_attempts"] == 1 and s["credits_estimated"] == 7500 and s["ambiguous_possible_charges"] == 0 and s["confirmed_remote_jobs"] == 1
    assert "?" not in s["per_provider"] and s["per_provider"]["dubvoice"]["paid"] == 1


def test_restart_after_adoption_makes_no_post(monkeypatch, tmp_path):
    pid, dub, goog = build_orphan(monkeypatch)
    vj.adopt_local(pid, SI, CI, file=local_video(tmp_path))
    trap = trap_generation(monkeypatch)
    T._restart()
    T.run(pid)
    T.run(pid, keys=[(SI, CI)])
    assert trap == [] and len(dub.calls) == 1 and not goog.calls and f5c(pid)["state"] == "ACCEPTED" and vj.paid_attempts(f5c(pid)) == 1


# ================================================================== rechazos seguros (sin tocar el estado)
def _unchanged(pid):
    f5 = f5c(pid)
    a = f5["attempts"][1]
    assert f5["state"] == "NEEDS_REVIEW" and a["status"] == "AMBIGUOUS" and not a.get("job_id") and not a.get("raw") and not a.get("adopted")
    assert vj.paid_attempts(f5) == 1 and f5["credits_spent"] == 7500


def test_missing_file_is_rejected_safely(monkeypatch, tmp_path):
    pid, dub, _ = build_orphan(monkeypatch)
    with pytest.raises(ValueError, match="no existe"):
        vj.adopt_local(pid, SI, CI, file=tmp_path / "nope.mp4")
    _unchanged(pid)


def test_directory_non_mp4_and_system_files_are_rejected(monkeypatch, tmp_path):
    pid, _, _ = build_orphan(monkeypatch)
    for bad in (tmp_path, Path("/etc"), Path("/etc/hostname"), Path("/etc/passwd"), tmp_path / "x.txt"):
        (tmp_path / "x.txt").write_text("hola")
        with pytest.raises(ValueError):
            vj.adopt_local(pid, SI, CI, file=bad)
    _unchanged(pid)
    assert not (store.pdir(pid) / "adopt_inbox").exists() or not list((store.pdir(pid) / "adopt_inbox").iterdir())          # nada se copio


def test_corrupt_mp4_is_rejected_before_touching_the_state(monkeypatch, tmp_path):
    pid, _, _ = build_orphan(monkeypatch)
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(os.urandom(8192))
    with pytest.raises(ValueError, match="valido|leer"):
        vj.adopt_local(pid, SI, CI, file=bad)
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"x" * 10)
    with pytest.raises(ValueError):
        vj.adopt_local(pid, SI, CI, file=tiny)
    _unchanged(pid)


def test_there_is_no_http_endpoint_that_accepts_local_paths(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    paths = [r.path for r in app.routes if "adopt" in getattr(r, "path", "")]
    assert paths == ["/api/projects/{pid}/videos/{i}/{j}/adopt"]                       # solo el de URL remota; no hay /adopt_local ni similar
    pid, dub, _ = build_orphan(monkeypatch)
    client = TestClient(app)
    for body in ({"file": "/etc/passwd"}, {"path": "/etc/passwd"}, {"local_file": "~/.ssh/id_rsa"}):
        assert client.post(f"/api/projects/{pid}/videos/{SI}/{CI}/adopt", json=body).status_code == 400            # sin result_url/job_id
    for bad_url in ("file:///etc/passwd", "/etc/passwd", "ftp://x/y.mp4", "http://example.com/v.mp4"):
        with pytest.raises(ValueError):
            vj.adopt_remote(pid, SI, CI, result_url=bad_url)
    _unchanged(pid)


# ================================================================== huella visual
def test_wrong_fingerprint_is_adoption_rejected_and_nothing_is_associated(monkeypatch, tmp_path):
    pid, dub, goog = build_orphan(monkeypatch)
    other = tmp_path / "green.mp4"
    media.run(["-f", "lavfi", "-i", "color=c=green:s=360x640:d=8:r=24", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "8", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-c:a", "aac", str(other)])
    trap = trap_generation(monkeypatch)
    vj.adopt_local(pid, SI, CI, file=other)
    f5 = f5c(pid)
    a = f5["attempts"][1]
    assert trap == [] and len(dub.calls) == 1 and not goog.calls
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "ADOPTION_REJECTED" and a["status"] == "ADOPTION_REJECTED"
    assert a["bad_raw"] and (store.pdir(pid) / a["bad_raw"]).exists() and not a.get("raw") and a["adoption_evidence"]["failed"]
    assert clip11(pid)["status"] == "error" and clip11(pid).get("raw") != a["bad_raw"] and clip11(pid).get("task_id") != a.get("job_id")
    assert vj.paid_attempts(f5) == 1 and f5["credits_spent"] == 7500 and a["resolved_ambiguity"]["from_status"] == "AMBIGUOUS"
    T._restart()
    T.run(pid)                                                                          # tras reiniciar: sin POST y el rechazo no se reintenta solo
    assert trap == [] and len(dub.calls) == 1 and f5c(pid)["state"] == "NEEDS_REVIEW"


def test_video_that_matches_another_scene_better_is_rejected(monkeypatch, tmp_path):
    pid, dub, _ = build_orphan(monkeypatch)
    other = tmp_path / "scene2.mp4"
    media.run(["-f", "lavfi", "-i", "color=c=0x228822:s=360x640:d=8:r=24", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "8", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-c:a", "aac", str(other)])
    media.extract_frame(other, 0.1, store.pdir(pid) / store.get(pid)["scenes"][1]["image"]["file"], width=270)     # la escena 2 se parece a ESE video
    vj.adopt_local(pid, SI, CI, file=other)
    ev = f5c(pid)["attempts"][1]["adoption_evidence"]
    assert f5c(pid)["review_reason"] == "ADOPTION_REJECTED" and any("otra escena" in x or "parecido" in x for x in ev["failed"])


def test_wrong_duration_is_rejected(monkeypatch, tmp_path):
    pid, _, _ = build_orphan(monkeypatch)
    short = tmp_path / "short.mp4"
    short.write_bytes(T.make_video(2))
    R.start_frame_from_video(pid, 0, video=T.make_video(2))
    vj.adopt_local(pid, SI, CI, file=short)
    ev = f5c(pid)["attempts"][1]["adoption_evidence"]
    assert f5c(pid)["review_reason"] == "ADOPTION_REJECTED" and any("duracion" in x for x in ev["failed"])


def test_same_file_cannot_be_adopted_by_two_clips(monkeypatch, tmp_path):
    pid, dub, _ = build_orphan(monkeypatch)
    p = local_video(tmp_path)
    vj.adopt_local(pid, SI, CI, file=p)
    with store.edit(pid) as q:                                                          # otro clip ambiguo pretende el mismo archivo
        c = q["scenes"][1]["clips"][0]
        c["f5"] = dict(vj._blank_f5(), state="NEEDS_REVIEW", review_kind="REMOTE", current_attempt="a1", attempts=[
            {"id": "a1", "round": 1, "paid": True, "status": "AMBIGUOUS", "provider": "dubvoice", "model": "veo-3.1-fast", "created_at": time.time()}])
    with pytest.raises(ValueError, match="ya esta asociado"):
        vj.adopt_local(pid, 1, 0, file=p)


# ================================================================== QC / audio normales
def test_visual_qc_and_audio_still_run_and_a_qc_failure_keeps_the_raw(monkeypatch, tmp_path):
    pid, dub, _ = build_orphan(monkeypatch)
    state = {"down": True}

    def flaky(content, **k):
        if state["down"]:
            raise RuntimeError("Anthropic respondio 529")
        return T.default_claude(content, **k)

    monkeypatch.setattr(claude, "ask_json", flaky)
    vj.adopt_local(pid, SI, CI, file=local_video(tmp_path))
    f5 = f5c(pid)
    a = f5["attempts"][1]
    assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "QC_UNAVAILABLE" and (store.pdir(pid) / a["raw"]).exists()       # NO ACCEPTED por dar una ruta
    assert vj.paid_attempts(f5) == 1 and len(dub.calls) == 1
    state["down"] = False
    T.run(pid)                                                                          # se reanuda desde el RAW, gratis
    assert f5c(pid)["state"] == "ACCEPTED" and len(dub.calls) == 1 and vj.paid_attempts(f5c(pid)) == 1


def test_wrong_dialogue_leaves_audio_needs_fix_but_the_video_is_accepted(monkeypatch, tmp_path):
    from app.services import stt
    pid, dub, _ = build_orphan(monkeypatch)
    monkeypatch.setattr(stt, "transcribe", lambda audio, settings=None, language_code=None: {"text": "completely unrelated words", "words": []})
    vj.adopt_local(pid, SI, CI, file=local_video(tmp_path))
    f5 = f5c(pid)
    assert f5["state"] == "ACCEPTED" and f5["visual_state"] == "OK" and f5["audio_state"] == "NEEDS_FIX" and len(dub.calls) == 1


def test_voice_change_runs_by_default_and_no_voice_defers_it(monkeypatch, tmp_path):
    calls = []

    def fake_voice(url, vid, progress=None, audio_path=None):
        calls.append(1)
        return T.mp3(4)

    monkeypatch.setattr(dubvoice, "voice_change", fake_voice)
    pid, dub, _ = build_orphan(monkeypatch, unify=True)
    vj.adopt_local(pid, SI, CI, file=local_video(tmp_path))
    assert len(calls) == 1 and f5c(pid)["state"] == "ACCEPTED" and f5c(pid)["audio_state"] in ("OK", "UNVERIFIED", "NEEDS_FIX")
    calls.clear()
    pid2, dub2, _ = build_orphan(monkeypatch, unify=True)
    vj.adopt_local(pid2, SI, CI, file=local_video(tmp_path), skip_voice=True)
    f5 = f5c(pid2)
    assert calls == [] and f5["state"] == "ACCEPTED" and f5["audio_state"] == "NEEDS_FIX" and "retry_voice" in (f5["audio_issue"] or "") + (clip11(pid2).get("warning") or "")


# ================================================================== CLI
def _cli():
    spec = importlib.util.spec_from_file_location("f5_adopt_local", Path(__file__).resolve().parents[1] / "tools" / "f5_adopt_local.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_adopts_reports_and_blocks_every_generation_function(monkeypatch, tmp_path, capsys):
    pid, dub, goog = build_orphan(monkeypatch)
    pid2, _, _ = build_orphan(monkeypatch)                                            # (antes de que el CLI bloquee la generacion)
    mod = _cli()
    saved = (videos.generate_raw, dubvoice.veo, google_veo.veo, dubvoice.balance)
    try:
        rc = mod.main([pid, str(local_video(tmp_path)), "--scene", str(SI), "--clip", str(CI)])
        out = capsys.readouterr().out
        assert rc == 0 and "RESULTADO: ACCEPTED" in out and "paid_attempts F5 = 1" in out and "creditos del clip = 7500" in out
        assert '"target_duration": 2.79' in out and '"raw_exists": true' in out and "ADOPTION" not in out and "BLOQUEADO" not in out
        for fn in (videos.generate_raw, dubvoice.veo, google_veo.veo):                 # el CLI deja bloqueadas las funciones de generacion
            with pytest.raises(RuntimeError, match="BLOQUEADO"):
                fn()
        rep = mod.report(pid, SI, CI)
        assert rep["accounting"]["attempt_ids"] == ["legacy", "a2"] and rep["attempt"]["resolved_ambiguity"]["from_status"] == "AMBIGUOUS"
        rc = mod.main([pid2, str(tmp_path / "missing.mp4"), "--scene", str(SI), "--clip", str(CI)])
        assert rc == 2 and "RECHAZADO" in capsys.readouterr().out
    finally:
        videos.generate_raw, dubvoice.veo, google_veo.veo, dubvoice.balance = saved
        vj._overrides.pop("fallback_provider", None)
    assert len(dub.calls) >= 1 and not goog.calls

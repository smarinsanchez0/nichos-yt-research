"""PASO 3.2.3 · Content-Encoding defectuoso en la respuesta del POST de DubVoice (canario real: DecodingError tras 106 s).

Servidor HTTP local en 127.0.0.1 (0 red real, 0 creditos). Cada caso cuenta los POST recibidos: la recuperacion NUNCA reenvia.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import test_f5_jobs as T
from app import store
from app.services import dubvoice, http as H
from app.services.errors import ErrorType, F5Error

env = T.env
SIGNED = "SIG-SECRET-TOKEN-123"


def _deflate_zlib(b):
    return zlib.compress(b)


def _deflate_raw(b):
    c = zlib.compressobj(wbits=-15)
    return c.compress(b) + c.flush()


MODES = {                      # modo -> (cabecera Content-Encoding, funcion sobre los bytes JSON)
    "plain": (None, lambda b: b),
    "gzip_ok": ("gzip", gzip.compress),
    "deflate_ok": ("deflate", _deflate_zlib),
    "deflate_raw": ("deflate", _deflate_raw),
    "gzip_label_plain": ("gzip", lambda b: b),
    "deflate_label_plain": ("deflate", lambda b: b),
    "corrupt": ("gzip", lambda b: b"\x1f\x8b\x08garbage-not-gzip\x00\xff"),
    "corrupt_binary": ("gzip", lambda b: b"\x00\x01\x02\x03binary"),
}


class Srv:
    def __init__(self, mode, delay=0.0, payload=None):
        self.mode, self.delay, self.posts, self.gets, self.accept = mode, delay, 0, 0, []
        self.payload = payload or {"task_id": "srv-job-1", "status": "processing"}
        outer = self

        class Hd(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, body, enc=None, ctype="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if enc:
                    self.send_header("Content-Encoding", enc)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.posts += 1
                outer.accept.append(self.headers.get("Accept-Encoding"))
                time.sleep(outer.delay)
                enc, fn = MODES[outer.mode]
                self._send(fn(json.dumps(outer.payload).encode()), enc)

            def do_GET(self):
                outer.gets += 1
                if self.path.startswith("/files/"):
                    return self._send(T.make_video(8), ctype="video/mp4")
                self._send(json.dumps({"task_id": "srv-job-1", "status": "completed", "video_url": f"http://127.0.0.1:{outer.port}/files/x.mp4"}).encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Hd)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def serve(monkeypatch):
    made = []

    def mk(mode, **kw):
        s = Srv(mode, **kw)
        made.append(s)
        monkeypatch.setattr(dubvoice, "BASE", f"http://127.0.0.1:{s.port}")
        monkeypatch.setenv("DUBVOICE_API_KEY", "k-test")
        dubvoice._poll_cache.clear()
        return s
    yield mk
    for s in made:
        s.close()


def once(s, **kw):
    return H.request_once("POST", f"http://127.0.0.1:{s.port}/api/v1/video", json={"a": 1}, **kw)


# ---------------- A-E: capa HTTP
@pytest.mark.parametrize("mode", ["plain", "gzip_ok", "deflate_ok", "deflate_raw", "gzip_label_plain", "deflate_label_plain"])
def test_A_to_E_every_encoding_is_decoded_or_recovered_with_one_post(serve, mode):
    s = serve(mode)
    r = once(s)
    assert r.status_code == 200 and r.json()["task_id"] == "srv-job-1" and r.decode_error is None
    assert s.posts == 1                                                           # J
    assert r.content_encoding == MODES[mode][0]
    if mode.endswith("label_plain"):
        assert r.decode_note and "plano" in r.decode_note                          # D/E: recuperado, anotado


def test_the_old_wrapper_really_failed_on_valid_gzip():
    import httpx
    with pytest.raises(httpx.DecodingError):                                      # causa raiz: re-envolver bytes ya decodificados
        httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b'{"a":1}')


@pytest.mark.parametrize("mode", ["corrupt", "corrupt_binary"])
def test_F_corrupt_body_keeps_status_headers_raw_and_flags_decode_error_without_raising(serve, mode):
    s = serve(mode)
    r = once(s)
    assert r.status_code == 200 and r.decode_error and r.raw and r.headers["content-type"] == "application/json"
    assert s.posts == 1


@pytest.mark.parametrize("mode", ["corrupt", "corrupt_binary"])
def test_F_corrupt_2xx_in_veo_is_invalid_response_ambiguous_one_post_no_retry(serve, mode, tmp_path):
    s = serve(mode)
    pid = T.make_project()
    rec = dubvoice.ContractRecorder(pid, "dubvoice", 0, 0, "a1")
    with pytest.raises(F5Error) as ei:
        T.strict_veo(tmp_path, recorder=rec)
    e = ei.value
    assert e.etype == ErrorType.INVALID_RESPONSE and e.ambiguous and e.sub == "undecodable_body"
    assert s.posts == 1
    sub = [json.loads(l) for l in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines() if '"submit"' in l][0]
    assert sub["http_status"] == 200 and sub["body_decode_error"] and sub["content_encoding"] == "gzip" and sub["raw_head_hex"]


# ---------------- G/H: recuperacion hasta el ContractRecorder y extraccion de job_id / result_url
@pytest.mark.parametrize("mode", ["gzip_label_plain", "deflate_label_plain", "gzip_ok"])
def test_G_recovered_response_reaches_recorder_and_job_id_is_extracted(serve, mode, tmp_path):
    s = serve(mode)
    pid = T.make_project()
    rec = dubvoice.ContractRecorder(pid, "dubvoice", 0, 0, "a1")
    seen = []
    tid, data = T.strict_veo(tmp_path, recorder=rec, on_submit=lambda j, m=None: seen.append((j, m)))
    assert tid == "srv-job-1" and data[:4] != b"" and seen[0][0] == "srv-job-1"
    assert s.posts == 1 and s.gets >= 1                                           # J: el resto es solo GET
    sub = [json.loads(l) for l in (store.pdir(pid) / "f5_contract.jsonl").read_text().splitlines() if '"op": "submit"' in l][0]
    assert sub["http_status"] == 200 and sub["content_type"] == "application/json" and sub["extracted"] and sub["body"]["task_id"] == "srv-job-1"
    assert sub["request_accept_encoding"] == "identity" and "content-type" in sub["header_names"]


def test_H_signed_url_is_redacted_in_the_contract_log(serve, tmp_path):
    url = f"http://127.0.0.1:1/f.mp4?X-Goog-Signature={SIGNED}&token={SIGNED}"
    s = serve("gzip_label_plain", payload={"video_url": url, "status": "completed"})
    pid = T.make_project()
    rec = dubvoice.ContractRecorder(pid, "dubvoice", 0, 0, "a1")
    with pytest.raises(Exception):                         # la descarga apunta a un puerto cerrado: solo nos importa el log
        T.strict_veo(tmp_path, recorder=rec)
    assert s.posts == 1
    assert SIGNED not in (store.pdir(pid) / "f5_contract.jsonl").read_text()


# ---------------- I: sin timeout artificial de 60 s
def test_I_slow_post_then_defective_encoding_is_recovered_within_the_configured_deadline(serve):
    s = serve("gzip_label_plain", delay=1.5)
    t0 = time.time()
    r = once(s, read=5, deadline=5)
    assert time.time() - t0 >= 1.5 and r.json()["task_id"] == "srv-job-1" and s.posts == 1
    import inspect
    assert "60" not in inspect.getsource(dubvoice._veo_strict)            # el limite sale de lim["submission"], no de un 60 fijo


# ---------------- Accept-Encoding: identity
def test_identity_is_requested_by_default_and_server_compression_is_still_safe(serve):
    s = serve("gzip_ok")                                                           # el servidor IGNORA identity y comprime igualmente
    r = once(s)
    assert s.accept == ["identity"] and r.request_accept_encoding == "identity" and r.json()["task_id"] == "srv-job-1"
    s2 = serve("plain")
    H.request_once("POST", f"http://127.0.0.1:{s2.port}/x", json={}, headers={"accept-encoding": "gzip"})
    assert s2.accept == ["gzip"]                                                   # no se pisa un valor explicito del llamador


def test_download_requests_identity(serve):
    s = serve("plain")
    seen = []
    orig = s.srv.RequestHandlerClass.do_GET

    def spy(self):
        seen.append(self.headers.get("Accept-Encoding"))
        return orig(self)
    s.srv.RequestHandlerClass.do_GET = spy
    assert H.download(f"http://127.0.0.1:{s.port}/files/x.mp4")
    assert seen == ["identity"]


# ---------------- credito: el total suma TODO el proyecto, el legacy no cuenta
def test_summary_sums_whole_project_by_clip_and_excludes_legacy():
    from app.phases import video_jobs as vj
    p = {"scenes": [{"clips": [
        {"status": "done", "f5": {"state": "ACCEPTED", "round": 1, "attempts": [
            {"id": "a1", "legacy": True, "paid": True, "credits": 7500, "status": "VALIDATED", "round": 1},
            {"id": "a2", "paid": True, "credits": 7500, "status": "VALIDATED", "round": 1, "job_id": "j"}]}},
        {"status": "done", "f5": {"state": "ACCEPTED", "round": 1, "attempts": [
            {"id": "a1", "paid": True, "credits": 7500, "status": "VALIDATED", "round": 1, "job_id": "k"}]}}]}]}
    s = vj.summarize(p)
    assert s["credits_estimated"] == 15000 and s["credits_by_clip"] == {"1.1": 7500, "1.2": 7500} and s["paid_attempts"] == 2

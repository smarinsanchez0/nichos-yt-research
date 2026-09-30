"""Prueba de extremo a extremo de las 6 fases con servicios externos simulados (sin gastar API).

Ejecuta: pytest -q     (usa ffmpeg real para escenas, recortes de silencio y subtitulos)
"""
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

os.environ["ERN_DATA_DIR"] = tempfile.mkdtemp(prefix="ern-test-")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, media  # noqa: E402
from app.services import claude, dubvoice, eleven, gemini, kie, stt  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ern-media-"))
SCRIPT = ("Did you know that ginger tea can calm your stomach in minutes. "
          "Grandma used this simple remedy every single day. "
          "Today I will show you three natural recipes that actually work. "
          "Get the free book at the link below and start now")


def make_source(path: Path):
    """15 s, 3 escenas de color, tono con pausas (simula voz)."""
    media.run(["-f", "lavfi", "-i", "color=c=red:s=360x640:d=5:r=30", "-f", "lavfi", "-i", "color=c=green:s=360x640:d=5:r=30",
               "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=5:r=30",
               "-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*220*t)*lt(mod(t,4),2.5)':d=15:s=44100",
               "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])


def fake_video(prompt: str) -> bytes:
    out = TMP / f"veo_{abs(hash(prompt))}.mp4"
    media.run(["-f", "lavfi", "-i", "testsrc=s=360x640:d=8:r=24", "-f", "lavfi", "-i",
               "aevalsrc='0.5*sin(2*PI*300*t)*lt(t,4.2)':d=8:s=44100", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", str(out)])
    return out.read_bytes()


def fake_ask_json(content, *, system="", model="", max_tokens=0):
    if isinstance(content, str):
        if content.startswith("Traduce"):
            rows = [l.split("\t", 1) for l in content.split("\n\n", 1)[1].splitlines()]
            return {"translations": [{"i": int(i), "es": "ES: " + t} for i, t in rows]}
        if content.startswith("Avatar:"):
            items = json.loads(content.split("ESCENAS:\n", 1)[1])
            return {"scenes": [{"n": it["n"], "image_prompt": f"prompt img {it['n']}", "dialogue_es": "es"} for it in items]}
        if content.startswith("Del siguiente guion"):
            return {"keywords": ["ginger", "free", "three"]}
    else:
        txt = content[-1]["text"]
        if txt.startswith("Analiza a la persona"):
            return {"description": "a man", "gender": "male", "age_range": "35-45",
                    "voice": {"gender": "male", "age": "middle_aged", "tone": "warm"}, "summary_es": "ok"}
        if txt.startswith("Te muestro"):
            n = int(re.search(r"(\d+) escena", txt).group(1))
            nums = [int(x) for x in re.findall(r"ESCENA (\d+)", " ".join(b["text"] for b in content if b["type"] == "text"))]
            assert len(nums) == n
            return {"scenes": [{"n": k, "shot": "medium", "person": "talks", "setting": "kitchen with US flag", "props": [],
                                "on_screen_text": "", "lighting": "warm", "motion": "none", "summary_es": "cocina"} for k in nums]}
        if "Para CADA clip" in txt:
            items = json.loads(txt.split("\n\n", 1)[1])
            return {"scenes": [{"scene": it["scene"], "clips": [
                {"clip": c["clip"], "camera": "Medium shot.", "action_en": "Smiles and gestures.", "delivery": "warm",
                 "action_es": "sonrie"} for c in it["clips"]]} for it in items]}
    raise AssertionError("prompt inesperado: " + str(content)[:200])


def fake_transcribe(audio, settings=None, language_code=None):
    d = media.probe(audio)["duration"]
    words = SCRIPT.split()
    step = d / len(words)
    return {"language": "en", "text": SCRIPT, "words": [
        {"text": w, "start": i * step, "end": i * step + step * 0.8} for i, w in enumerate(words)]}


@pytest.fixture(scope="module")
def client():
    os.environ["ELEVENLABS_API_KEY"] = os.environ["KIE_API_KEY"] = os.environ["GOOGLE_API_KEY"] = "test"
    claude.ask_json = fake_ask_json
    stt.transcribe = fake_transcribe
    eleven.list_voices = lambda: [
        {"voice_id": "v1", "name": "Ana", "category": "premade", "gender": "female", "age": "young", "accent": "american",
         "descriptive": "warm", "use_case": "social media", "preview_url": None},
        {"voice_id": "v2", "name": "Bob", "category": "premade", "gender": "male", "age": "middle aged", "accent": "american",
         "descriptive": "warm", "use_case": "conversational", "preview_url": None}]
    dubvoice.list_voices = lambda gender=None, language="en", n=40: [
        v for v in eleven.list_voices() if not gender or v["gender"] == gender]
    kie.upload_file = lambda path, mime="audio/mpeg", folder="": "https://example.com/a.mp3"
    dubvoice.voice_change = lambda url, vid, progress=None: _mp3(8)
    gemini.generate_image = lambda prompt, refs, model, aspect="9:16": _jpg()
    kie.upload_image = lambda path: "https://example.com/x.jpg"
    kie.veo_generate = lambda prompt, url, model="veo3_fast", aspect="9:16", progress=None, timeout=0: ("task1", fake_video(prompt))
    from app.main import app
    return TestClient(app)


def _jpg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (576, 1024), (120, 90, 60)).save(b, "JPEG")
    return b.getvalue()


def _mp3(sec: float) -> bytes:
    f = TMP / "v.mp3"
    media.run(["-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*500*t)*lt(t,4.2)':d={sec}:s=44100", str(f)])
    return f.read_bytes()


def wait(client, pid, job, timeout=240):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/projects/{pid}").json()["jobs"].get(job, {})
        if j.get("status") in ("done", "error"):
            return j
        time.sleep(0.5)
    raise TimeoutError(job)


def test_full_pipeline(client):
    pid = client.post("/api/projects", json={"name": "t"}).json()["id"]
    # Fase 1
    r = client.post(f"/api/projects/{pid}/avatar", files={"file": ("a.jpg", _jpg(), "image/jpeg")})
    assert r.status_code == 200
    assert wait(client, pid, "avatar")["status"] == "done"
    # Fase 2
    src = TMP / "src.mp4"
    make_source(src)
    r = client.post(f"/api/projects/{pid}/video", files={"file": ("src.mp4", src.read_bytes(), "video/mp4")})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200
    j = wait(client, pid, "analysis")
    assert j["status"] == "done", j
    p = client.get(f"/api/projects/{pid}").json()
    assert all(p["analysis"]["points"].values())
    assert len(p["scenes"]) >= 3, [(s["start"], s["end"]) for s in p["scenes"]]
    assert all(s["image_prompt"] and len(s["frames"]) == 3 for s in p["scenes"])
    n = len(p["scenes"])
    # Fase 4 antes de aprobar imagenes debe fallar
    assert client.post(f"/api/projects/{pid}/fragment").status_code == 400
    # Fase 3
    assert client.post(f"/api/projects/{pid}/images/generate").status_code == 200
    assert wait(client, pid, "images")["status"] == "done"
    assert client.post(f"/api/projects/{pid}/images/0/regenerate", json={"mode": "edit", "notes": "sonrie mas"}).status_code == 200
    assert wait(client, pid, "img:0")["status"] == "done"
    p = client.get(f"/api/projects/{pid}").json()
    assert len(p["scenes"][0]["versions"]) == 2 and not p["scenes"][0]["image"]["approved"]
    client.post(f"/api/projects/{pid}/images/approve_all")
    # Fase 4
    assert client.post(f"/api/projects/{pid}/fragment").status_code == 200
    j = wait(client, pid, "fragment")
    assert j["status"] == "done", j
    p = client.get(f"/api/projects/{pid}").json()
    said = " ".join(c["dialogue"] for s in p["scenes"] for c in s["clips"])
    assert said.split() == SCRIPT.split(), "el guion fragmentado debe cubrir todas las palabras, en orden"
    assert all(c["video_prompt"] for s in p["scenes"] for c in s["clips"])
    # Fase 5
    assert client.post(f"/api/projects/{pid}/videos/generate").status_code == 400  # falta la voz
    v = client.get(f"/api/projects/{pid}/voices").json()
    assert v["voices"][0]["voice_id"] == "v2"  # avatar masculino -> voz masculina primero
    client.patch(f"/api/projects/{pid}/settings", json={"voice_id": "v2", "voice_name": "Bob"})
    assert client.post(f"/api/projects/{pid}/videos/generate").status_code == 200
    j = wait(client, pid, "videos")
    assert j["status"] == "done", j
    p = client.get(f"/api/projects/{pid}").json()
    assert all(c["status"] == "done" for s in p["scenes"] for c in s["clips"])
    # Fase 6
    assert client.post(f"/api/projects/{pid}/edit").status_code == 200
    j = wait(client, pid, "edit")
    assert j["status"] == "done", j
    p = client.get(f"/api/projects/{pid}").json()
    out = Path(os.environ["ERN_DATA_DIR"]) / "projects" / pid / p["final"]["file"]
    info = media.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    total_raw = sum(c["duration"] for s in p["scenes"] for c in s["clips"])
    assert info["duration"] < total_raw - 1, "debe haber recortado silencios"
    ass = (Path(os.environ["ERN_DATA_DIR"]) / "projects" / pid / p["final"]["subs"]).read_text()
    assert "Poppins" in ass and r"\c&H00FFFF&" in ass and "GINGER" in ass
    assert p["final"]["script_match"] > 0.9
    print("duracion final", info["duration"], "de", total_raw, "escenas", n)


def test_dubvoice_adapter(monkeypatch, tmp_path):
    """El adaptador de DubVoice envia refs en base64, sondea y descarga (HTTP simulado)."""
    from app.services import dubvoice
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    img = tmp_path / "a.jpg"
    img.write_bytes(_jpg())
    calls = []

    class R:
        def __init__(self, code, j): self.status_code, self._j, self.text = code, j, str(j)
        def json(self): return self._j

    def fake_request(method, url, **kw):
        calls.append((method, url, kw))
        if method == "POST" and url.endswith("/api/v1/video"):
            assert kw["json"]["aspect_ratio"] == "9:16" and kw["json"]["ref_images"][0].startswith("data:image/jpeg;base64,")
            return R(200, {"task_id": "abc"})
        if url.endswith("/api/v1/video"):
            return R(200, {"status": "completed", "result": "https://x/v.mp4"})
        if url.endswith("/api/image-generate"):
            return R(200, {"success": True, "id": "img1"})
        if url.endswith("/status"):
            return R(200, {"status": "succeeded", "image_url": "https://x/i.png"})
        raise AssertionError(url)

    monkeypatch.setattr(dubvoice, "request", fake_request)
    monkeypatch.setattr(dubvoice, "download", lambda u: b"DATA:" + u.encode())
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    assert dubvoice.veo("p", img)[1] == b"DATA:https://x/v.mp4"
    assert dubvoice.image("p", [img]) == b"DATA:https://x/i.png"


def test_local_whisper_adapter(monkeypatch, tmp_path):
    import types
    import faster_whisper
    w = lambda t, a, b: types.SimpleNamespace(word=f" {t}", start=a, end=b)

    class FakeModel:
        def __init__(self, *a, **k): pass
        def transcribe(self, path, **k):
            assert k["word_timestamps"] is True
            return iter([types.SimpleNamespace(words=[w("Hello,", 0, .4), w("world.", .5, 1)])]), types.SimpleNamespace(language="en")

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    stt._models.clear()
    r = stt.transcribe_local(tmp_path / "x.mp3")
    assert [x["text"] for x in r["words"]] == ["Hello,", "world."] and r["language"] == "en"


def test_dubvoice_voice_changer(monkeypatch):
    import importlib
    importlib.reload(dubvoice)   # el fixture del pipeline sustituyo funciones de este modulo
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")

    class R:
        def __init__(self, j): self.status_code, self._j, self.text = 200, j, str(j)
        def json(self): return self._j

    def fake(method, url, **kw):
        if method == "POST":
            assert kw["json"] == {"audio_url": "https://a/x.mp3", "target_voice_id": "v9"}
            return R({"task_id": "t1"})
        return R({"status": "completed", "result": "https://a/out.mp3"})

    monkeypatch.setattr(dubvoice, "request", fake)
    monkeypatch.setattr(dubvoice, "download", lambda u: b"AUDIO")
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    dubvoice._poll_cache.clear()
    assert dubvoice.voice_change("https://a/x.mp3", "v9") == b"AUDIO"

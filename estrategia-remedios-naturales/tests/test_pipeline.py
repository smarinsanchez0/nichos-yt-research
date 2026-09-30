"""Prueba de extremo a extremo de las 6 fases con servicios externos simulados (sin gastar API).

Ejecuta: pytest -q     (usa ffmpeg real para escenas, recortes de silencio y subtitulos)
"""
from __future__ import annotations
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ERN_DATA_DIR", tempfile.mkdtemp(prefix="ern-test-"))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, media  # noqa: E402
from app.services import claude, dubvoice, eleven, gemini, google_veo, kie, stt  # noqa: E402

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
        if content.startswith("Traduce cada linea") or content.startswith("Traduce las siguientes") or (content.startswith("Traduce") and "natural cada" not in content[:40]):
            rows = [l.split("\t", 1) for l in content.split("\n\n", 1)[1].splitlines()]
            return {"translations": [{"i": int(i), "es": "ES: " + t} for i, t in rows]}
        if content.startswith("Traduce al español natural"):
            items = json.loads(content.split("\n\n", 1)[1])
            return {"scenes": [{"n": it["n"], "dialogue_es": "es"} for it in items]}
        if content.startswith("Del siguiente guion"):
            return {"keywords": ["ginger", "free", "three"]}
    else:
        txt = content[-1]["text"]
        if txt.startswith("META-PROMPT"):
            close = "Use the provided character image for the person's appearance, face, and clothing exactly as shown." \
                if txt.startswith("META-PROMPT 1") else \
                "Use Image A for the specific action and Image B for the character appearance and environment continuity."
            return {"image_prompt": "Vertical 9:16 shot. " + close, "action": "Points at the chest"}
        if txt.startswith("Analiza a la persona"):
            return {"description": "a man", "gender": "male", "age_range": "35-45",
                    "voice": {"gender": "male", "age": "middle_aged", "tone": "warm"}, "summary_es": "ok"}
        if txt.startswith("Te muestro"):
            n = int(re.search(r"(\d+) escena", txt).group(1))
            nums = [int(x) for x in re.findall(r"ESCENA (\d+)", " ".join(b["text"] for b in content if b["type"] == "text"))]
            assert len(nums) == n
            return {"scenes": [{"n": k, "shot": "medium", "person": "talks", "setting": "kitchen with US flag", "props": [],
                                "on_screen_text": "", "lighting": "warm", "motion": "none", "summary_es": "cocina"} for k in nums]}
        if txt.startswith("QC VISUAL"):
            return {"visual_ok": True, "reasons": [], "prompt_fix": ""}
        if "Para CADA clip" in txt:
            items = json.loads(txt.split("\n\n", 1)[1])
            return {"scenes": [{"scene": it["scene"], "clips": [
                {"clip": c["clip"], "camera": "Medium shot.", "action_en": "Smiles and gestures.", "delivery": "warm",
                 "action_es": "sonrie", "dialogue_es": "ES " + c["dialogue_en"]} for c in it["clips"]]} for it in items]}
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
    dubvoice.voice_change = lambda url, vid, progress=None, audio_path=None: _mp3(8)
    gemini.generate_image = lambda prompt, refs, model, aspect="9:16": _jpg()
    kie.upload_image = lambda path: "https://example.com/x.jpg"
    dubvoice.veo = lambda prompt, image_path, model="veo-3.1-fast", aspect="9:16", resolution="720p", progress=None, timeout=0, duration=None, cancel=None: ("task1", fake_video(prompt))
    google_veo.veo = lambda prompt, image_path, model="x", aspect="9:16", duration=8, resolution="720p", progress=None, timeout=0, cancel=None: ("g", fake_video(prompt))
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
    assert all(s["action"].startswith("Points") for s in p["scenes"])
    n = len(p["scenes"])
    # Fase 4 antes de aprobar imagenes debe fallar
    assert client.post(f"/api/projects/{pid}/fragment").status_code == 400
    client.patch(f"/api/projects/{pid}/settings", json={"output_language": "en"})   # el flujo completo se prueba en ingles
    # Fase 3
    assert client.post(f"/api/projects/{pid}/images/generate").status_code == 200
    j = wait(client, pid, "images")
    assert j["status"] == "done" and j["message"].startswith("ℹ️"), j     # metodo de la guia: primero solo el start frame
    p = client.get(f"/api/projects/{pid}").json()
    assert p["scenes"][0].get("image") and not any(s.get("image") for s in p["scenes"][1:])
    assert p["scenes"][0]["image_prompt"].endswith("exactly as shown.") and p["scenes"][1]["image_prompt"].endswith("environment continuity.")
    client.post(f"/api/projects/{pid}/images/0/approve", json={"approved": True})
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
    import importlib
    importlib.reload(dubvoice)   # el fixture del pipeline sustituyo funciones de este modulo
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


def test_voice_change_falls_back_across_call_styles(monkeypatch, tmp_path):
    import importlib
    importlib.reload(dubvoice)
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    aud = tmp_path / "a.mp3"; aud.write_bytes(b"ID3xx")
    calls = []

    class R:
        def __init__(self, code, body=b"", ctype="application/json", j=None):
            self.status_code, self.content, self.text, self._j = code, body, "err", j
            self.headers = {"content-type": ctype}
        def json(self): return self._j or {}

    def fake(method, url, **kw):
        calls.append((method, url, "X-API-Key" in (kw.get("headers") or {})))
        if "/api/v1/voice-changer" in url:
            return R(401, j={"error": "Unauthorized"})
        if url.endswith("/api/voice-changer"):
            return R(200, b"ID3AUDIO", "audio/mpeg")
        raise AssertionError(url)

    monkeypatch.setattr(dubvoice, "request", fake)
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    assert dubvoice.voice_change("https://a/x.mp3", "v9", audio_path=aud) == b"ID3AUDIO"
    assert any(c[2] for c in calls if "v1" in c[1]), "debe reintentar con X-API-Key"
    assert dubvoice._voice_mode == ["upload-bearer"]


def test_dubvoice_retries_on_429(monkeypatch):
    import importlib
    importlib.reload(dubvoice)
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    n = {"i": 0}

    class R:
        def __init__(self, code, j=None): self.status_code, self._j, self.text, self.headers = code, j or {}, "x", {}
        def json(self): return self._j

    def fake(method, url, **kw):
        if method == "POST":
            n["i"] += 1
            return R(429) if n["i"] < 3 else R(200, {"image_url": "https://x/i.png", "status": "completed"})

    monkeypatch.setattr(dubvoice, "request", fake)
    monkeypatch.setattr(dubvoice, "download", lambda u: b"IMG")
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    assert dubvoice.image("p", []) == b"IMG" and n["i"] == 3


def test_image_fallback_when_dubvoice_fails(monkeypatch, tmp_path):
    from app.phases import images
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setattr(images.time, "sleep", lambda s: None)

    def boom(*a, **k): raise RuntimeError("DubVoice (imagen) tardo demasiado (timeout)")

    monkeypatch.setattr(images.dubvoice, "image", boom)
    monkeypatch.setattr(images.gemini, "generate_image", lambda *a, **k: b"OK")
    p = {"settings": {"image_provider": "dubvoice", "image_fallback": True, "dubvoice_image_model": "nano-banana-pro",
                      "image_model": "m", "kie_image_model": "k"}}
    data, used, errs = images._call(p, "prompt", [("l", tmp_path / "a.jpg")])
    assert data == b"OK" and used == "google" and "dubvoice" in errs[0]


def test_spanish_output_and_blurred_layout(client):
    """Idioma final espanol: el dialogo del clip va en espanol y el frame de referencia va difuminado."""
    from app.phases import fragment, images
    from app import store
    pid = client.post("/api/projects", json={"name": "es"}).json()["id"]
    c = {"dialogue": "Hola a todos", "dialogue_en": "Hello everyone", "lang": "es", "camera": "Close-up.",
         "action_en": "Smiles.", "delivery": "warm"}
    vp = fragment.build_prompt(c)
    assert "Hola a todos" in vp and vp.endswith("Continúa desde ahí.") and "Grabado con iPhone" in vp and "diciendo en español" in vp
    words = [{"text": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(40)]
    assert all(len(ch) <= 17 for ch in fragment.chunk_words(words, "es"))
    # capa difuminada: sin detalle fino (muy baja frecuencia)
    src = store.path(pid, "frames", "f.jpg")
    im = Image.new("RGB", (360, 640), (200, 200, 200))
    for x in range(0, 360, 4):
        for y in range(100, 200):
            im.putpixel((x, y), (0, 0, 0))   # rayas finas simulan cara/ropa
    im.save(src)
    out = images._layout_ref(pid, {"idx": 0, "frame": store.rel(pid, src)})
    px = Image.open(out).convert("L")
    row = [px.getpixel((x, 150)) for x in range(100, 200)]
    assert max(row) - min(row) < 60, "el frame de referencia debe quedar difuminado"


def test_image_prompt_leads_with_action(client):
    from app.phases import images
    from app import store
    pid = client.post("/api/projects", json={"name": "act"}).json()["id"]
    av = store.path(pid, "avatar", "a.jpg"); Image.new("RGB", (100, 100)).save(av)
    fr = store.path(pid, "frames", "f.jpg"); Image.new("RGB", (100, 180)).save(fr)
    p = {"id": pid, "avatar": {"file": store.rel(pid, av), "profile": {"description": "a woman in a blue dress"}},
         "settings": {"scene_ref_mode": "swap", "global_notes": ""},
         "scenes": [{"idx": 0, "frame": store.rel(pid, fr), "action": "Points at the lungs graphic with her right index finger",
                     "read": {"shot": "medium"}, "image_prompt": "x"}]}
    prompt, refs = images._compose_new(p, p["scenes"][0], "")
    assert prompt.startswith("EDIT IMAGE 1") and "Points at the lungs graphic" in prompt      # modo swap (por defecto)
    assert "ORIGINAL SCENE" in refs[0][0] and "AVATAR" in refs[1][0]
    p["settings"]["scene_ref_mode"] = "blur"
    prompt, refs = images._compose_new(p, p["scenes"][0], "")
    assert prompt.startswith("PRIMARY GOAL") and "Points at the lungs graphic" in prompt.split("\n")[0]
    assert "IMAGE 1" in refs[0][0] and "BLURRED" in refs[1][0]


def test_qa_retries_until_avatar_and_action_ok(client, monkeypatch):
    """Si la revision dice que no es el avatar / no hace la accion, regenera con la correccion (maximo 3 intentos)."""
    from app.phases import images
    from app import store
    pid = client.post("/api/projects", json={"name": "qa"}).json()["id"]
    av = store.path(pid, "avatar", "a.jpg"); Image.new("RGB", (100, 100)).save(av)
    fr = store.path(pid, "frames", "f.jpg"); Image.new("RGB", (100, 180)).save(fr)
    with store.edit(pid) as q:
        q["avatar"] = {"file": store.rel(pid, av), "profile": {"description": "farmer with straw hat"}}
        q["scenes"] = [{"idx": 0, "frame": store.rel(pid, fr), "action": "Points down", "read": {}, "image_prompt": "x"}]
    seen = []
    monkeypatch.setattr(images, "_call", lambda p, prompt, refs: (seen.append(prompt) or _jpg(), "google", []))
    answers = iter([{"ok": False, "fix": "Hands must point at the lungs, not be crossed.", "differences": "manos cruzadas"},
                    {"ok": True, "fix": "", "differences": ""}])
    monkeypatch.setattr(images, "_qa", lambda p, s, data: next(answers))
    images.generate(pid, 0)
    assert len(seen) == 2 and "Hands must point at the lungs" in seen[1] and "CORRECTION" in seen[1]
    assert store.get(pid)["scenes"][0]["image"]["qa"]["ok"] is True


def test_guide_chain_uses_previous_image_as_reference(client):
    from app.phases import images
    from app import store
    pid = client.post("/api/projects", json={"name": "chain"}).json()["id"]
    av = store.path(pid, "avatar", "a.jpg"); Image.new("RGB", (100, 100)).save(av)
    fr = [store.path(pid, "frames", f"f{i}.jpg") for i in range(2)]
    for f in fr: Image.new("RGB", (100, 180)).save(f)
    gen0 = store.path(pid, "images", "g0.jpg"); Image.new("RGB", (100, 180), (9, 9, 9)).save(gen0)
    p = {"id": pid, "avatar": {"file": store.rel(pid, av)}, "settings": {"global_notes": ""},
         "scenes": [{"idx": 0, "frame": store.rel(pid, fr[0]), "image": {"file": store.rel(pid, gen0)}, "image_prompt": "P0"},
                    {"idx": 1, "frame": store.rel(pid, fr[1]), "image_prompt": "P1"}]}
    _, refs0 = images._compose_guide(p, p["scenes"][0], "")
    assert [l.split(" -")[0] for l, _ in refs0] == ["Reference image", "Character image"]
    prompt, refs = images._compose_guide(p, p["scenes"][1], "que sonria")
    assert [l.split(" -")[0] for l, _ in refs] == ["Image A", "Image B", "Image C"]
    assert refs[1][1].name == "g0.jpg" and prompt.startswith("P1") and "que sonria" in prompt


def test_variable_duration_tiers():
    importlib_reload = __import__("importlib").reload
    importlib_reload(dubvoice)
    assert [dubvoice.tier_for(x) for x in (2.5, 3.9, 4.1, 5.5, 7, 8.2, 12)] == [4, 4, 6, 6, 8, 10, 10]
    assert dubvoice.tier_for(3, "veo-3.1-fast") == 8            # Veo siempre 8 s
    assert dubvoice.credits_for("omniflash", 3) == 4688 and dubvoice.credits_for("veo-3.1-fast", 3) == 7500


def test_omniflash_sends_duration(monkeypatch, tmp_path):
    import importlib
    importlib.reload(dubvoice)
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    img = tmp_path / "a.jpg"; img.write_bytes(_jpg())
    sent = {}

    class R:
        status_code = 200
        text = "x"
        def __init__(self, j): self._j = j
        def json(self): return self._j

    def fake(method, url, **kw):
        if method == "POST":
            sent.update(kw["json"]); return R({"task_id": "t"})
        return R({"status": "completed", "result": "https://x/v.mp4"})

    monkeypatch.setattr(dubvoice, "request", fake)
    monkeypatch.setattr(dubvoice, "download", lambda u: b"V")
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    dubvoice.veo("p", img, model="omniflash", duration=3.2)
    assert sent["model"] == "omniflash" and sent["duration"] == 4
    dubvoice._poll_cache.clear(); sent.clear()
    dubvoice.veo("p", img, model="veo-3.1-fast", duration=3.2)
    assert "duration" not in sent


def test_dubvoice_video_progress_and_timeout(monkeypatch, tmp_path):
    import importlib
    importlib.reload(dubvoice)
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    img = tmp_path / "a.jpg"; img.write_bytes(_jpg())
    msgs = []

    class R:
        status_code = 200
        text = "x"
        def __init__(self, j): self._j = j
        def json(self): return self._j

    monkeypatch.setattr(dubvoice, "request", lambda m, u, **k: R({"task_id": "t"}) if m == "POST" else R({"status": "processing"}))
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    clock = iter(range(0, 100000, 200))
    monkeypatch.setattr(dubvoice.time, "time", lambda: next(clock))
    try:
        dubvoice.veo("p", img, progress=msgs.append, timeout=600)
        assert False
    except RuntimeError as e:
        assert "no termino en 10 min" in str(e)
    assert any("generando" in m for m in msgs)


def _fast_f5(monkeypatch):
    from app.phases import video_jobs
    video_jobs.SCHED.reset_for_tests()
    video_jobs._overrides.clear()
    video_jobs._overrides.update(poll_interval=0.05, contract_canary=False, watchdog_margin=2)
    for name in ("WAIT_TIMEOUT_RETRY", "WAIT_AMBIGUOUS", "WAIT_UNKNOWN", "WAIT_DOWNLOAD"):
        monkeypatch.setattr(video_jobs, name, 0.0)
    return video_jobs


def test_supervisor_retries_failures_audits_and_finishes(client, monkeypatch):
    """Fase 5: un clip falla por politica de contenido; el MOTOR (no Claude) decide reintentar, Claude solo reescribe el prompt y hace el QC."""
    from app.phases import supervisor
    from app import store
    _fast_f5(monkeypatch)
    pid = client.post("/api/projects", json={"name": "sup"}).json()["id"]
    img = store.path(pid, "images", "i.jpg"); Image.new("RGB", (100, 180)).save(img)
    dlg = "ginger tea can calm your stomach"
    with store.edit(pid) as q:
        q["settings"].update(unify_voice=False, output_language="en", dubvoice_video_model="veo-3.1-fast")
        q["scenes"] = [{"idx": i, "image": {"file": store.rel(pid, img), "approved": True}, "frame": store.rel(pid, img),
                        "clips": [{"idx": 0, "dialogue": dlg, "video_prompt": "orig prompt", "target": 6, "status": "pending", "lang": "en"}]}
                       for i in range(2)]
    calls = {"veo": []}

    def fake_veo(prompt, image_path, model="veo-3.1-fast", aspect="9:16", resolution="720p", progress=None, timeout=0, duration=None, cancel=None):
        calls["veo"].append(prompt)
        if prompt == "orig prompt" and len([c for c in calls["veo"] if c == "orig prompt"]) == 1:
            raise RuntimeError("DubVoice (video) fallo: content policy violation")
        return "t", fake_video(prompt)

    def fake_sup(content, *, system="", model="", max_tokens=0):
        txt = content[-1]["text"]
        if txt.startswith("REESCRIBE PROMPT"):
            return {"prompt": "simplified prompt"}
        assert txt.startswith("QC VISUAL"), txt[:80]
        return {"visual_ok": True, "reasons": [], "prompt_fix": ""}

    monkeypatch.setattr(dubvoice, "veo", fake_veo)
    monkeypatch.setattr(google_veo, "veo", lambda prompt, image_path, **k: fake_veo(prompt, image_path))
    monkeypatch.setattr(claude, "ask_json", fake_sup)
    monkeypatch.setattr(stt, "transcribe", lambda audio, settings=None, language_code=None: {
        "text": dlg, "words": [{"text": w, "start": i * .5, "end": i * .5 + .4} for i, w in enumerate(dlg.split())]})
    supervisor.start(pid, lambda msg=None, progress=None: None)
    p = store.get(pid)
    clips = [c for s in p["scenes"] for c in s["clips"]]
    assert all(c["status"] == "done" and c["verified"] and c["f5"]["state"] == "ACCEPTED" for c in clips)
    assert "simplified prompt" in calls["veo"] and any(c["video_prompt"] == "simplified prompt" for c in clips)   # cual clip falla primero depende del paralelismo
    log = " ".join(x["msg"] for x in p["jobs"]["supervisor"]["log"])
    assert "Reintento" in log and "Aceptado" in log and "Terminado: 2/2" in log


def _setup_sup_project(client, n=2, dlg="ginger tea can calm your stomach"):
    from app import store
    pid = client.post("/api/projects", json={"name": "sup2"}).json()["id"]
    img = store.path(pid, "images", "i.jpg"); Image.new("RGB", (100, 180)).save(img)
    with store.edit(pid) as q:
        q["settings"].update(unify_voice=False, output_language="en", dubvoice_video_model="veo-3.1-fast")
        q["scenes"] = [{"idx": i, "image": {"file": store.rel(pid, img), "approved": True}, "frame": store.rel(pid, img),
                        "clips": [{"idx": 0, "dialogue": dlg, "video_prompt": "p", "target": 6, "status": "pending", "lang": "en"}]}
                       for i in range(n)]
    return pid


def test_supervisor_when_claude_is_down_keeps_the_raw_and_never_regenerates(client, monkeypatch):
    """Claude caido: el motor NO se cuelga ni destruye el RAW pagado ni regenera; deja el clip en revision y se reanuda gratis."""
    from app.phases import supervisor
    from app import store
    _fast_f5(monkeypatch)
    pid = _setup_sup_project(client)
    n = {"i": 0}

    def flaky_veo(prompt, image_path, model="veo-3.1-fast", aspect="9:16", resolution="720p", progress=None, timeout=0, duration=None, cancel=None):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("503 servicio caido")            # error desconocido → UN reintento del motor
        return "t", fake_video(prompt)

    def down(*a, **k): raise RuntimeError("Anthropic respondio 529")

    monkeypatch.setattr(dubvoice, "veo", flaky_veo)
    monkeypatch.setattr(claude, "ask_json", down)
    monkeypatch.setattr(stt, "transcribe", lambda audio, settings=None, language_code=None: {"text": "ginger tea can calm your stomach", "words": []})
    res = supervisor.start(pid, lambda msg=None, progress=None: None)
    clips = [c for s in store.get(pid)["scenes"] for c in s["clips"]]
    assert res["state"] == "COMPLETED_WITH_WARNINGS" and n["i"] == 3                      # 2 clips + 1 reintento; NADA se regenera por culpa de Claude
    for c in clips:
        f5 = c["f5"]
        assert f5["state"] == "NEEDS_REVIEW" and f5["review_reason"] == "QC_UNAVAILABLE"
        assert (store.pdir(pid) / f5["attempts"][-1]["raw"]).exists()
    monkeypatch.setattr(claude, "ask_json", lambda content, **k: {"visual_ok": True, "reasons": [], "prompt_fix": ""})
    supervisor.start(pid, lambda msg=None, progress=None: None)                            # Claude vuelve: se reanuda desde el RAW
    assert n["i"] == 3 and all(c["status"] == "done" and c["f5"]["state"] == "ACCEPTED" for s in store.get(pid)["scenes"] for c in s["clips"])


def test_supervisor_does_not_salvage_with_a_still_image_automatically(client, monkeypatch):
    """Decision de producto: la locucion sobre imagen fija es OPT-IN (endpoint /videos/salvage), jamas un fallback automatico."""
    from app.phases import supervisor, videos
    from app import store
    _fast_f5(monkeypatch)
    pid = _setup_sup_project(client, n=1)

    def always_fail(prompt, image_path, **k): raise RuntimeError("content policy")

    monkeypatch.setattr(dubvoice, "veo", always_fail)
    monkeypatch.setattr(google_veo, "veo", always_fail)
    monkeypatch.setattr(videos, "make_still_clip", lambda *a, **k: (_ for _ in ()).throw(AssertionError("imagen fija automatica")))
    monkeypatch.setattr(claude, "ask_json", lambda content, **k: {"prompt": "otro prompt " + str(time.time())})
    res = supervisor.start(pid, lambda msg=None, progress=None: None)
    c = store.get(pid)["scenes"][0]["clips"][0]
    assert res["state"] == "COMPLETED_WITH_WARNINGS" and c["f5"]["state"] == "NEEDS_REVIEW" and c["status"] == "error" and not c.get("file")
    # el usuario SI puede pedirla explicitamente
    monkeypatch.undo()
    monkeypatch.setattr(dubvoice, "edge_tts", lambda text, voice="x": _mp3(4))
    videos.make_still_clip(pid, 0, 0)
    c = store.get(pid)["scenes"][0]["clips"][0]
    assert c["status"] == "done" and c["provider_used"] == "still" and c["f5"]["state"] == "ACCEPTED" and c["f5"]["accepted_via"] == "still_image_optin"
    info = media.probe(Path(os.environ["ERN_DATA_DIR"]) / "projects" / pid / c["file"])
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"] and info["duration"] > 2


def test_google_quota_error_disables_google_and_there_is_no_model_ladder(client, monkeypatch):
    from app.phases import supervisor
    from app import store
    from app.services.errors import ErrorType, F5Error
    _fast_f5(monkeypatch)
    pid = _setup_sup_project(client, n=1)
    seen = []

    def dub(prompt, image_path, model="veo-3.1-fast", **k):
        seen.append(("dubvoice", model))
        raise F5Error(ErrorType.PROVIDER_TIMEOUT, "DubVoice (video) no termino en 10 min", provider="dubvoice", job_id=f"j{len(seen)}")

    def goog(prompt, image_path, **k):
        seen.append(("google", None))
        raise RuntimeError("Google Veo respondio 429: RESOURCE_EXHAUSTED You exceeded your current quota")

    monkeypatch.setattr(dubvoice, "veo", dub)
    monkeypatch.setattr(google_veo, "veo", goog)
    res = supervisor.start(pid, lambda msg=None, progress=None: None)
    c = store.get(pid)["scenes"][0]["clips"][0]
    assert [x[0] for x in seen] == ["dubvoice", "dubvoice", "google"] and {x[1] for x in seen if x[0] == "dubvoice"} == {"veo-3.1-fast"}   # sin lite/omniflash/meta
    assert c["f5"]["state"] == "NEEDS_REVIEW" and res["state"] == "COMPLETED_WITH_WARNINGS"


def test_cli_demo_end_to_end_delivers_final_video(tmp_path):
    """La skill /remedios usa este comando: video + avatar -> MP4 final 1080x1920 con reporte (servicios simulados)."""
    import subprocess, sys
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "app.cli", "run", "--demo", "--name", "t", "--out", str(tmp_path)],
                       cwd=root, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    res = json.loads([l for l in r.stdout.splitlines() if l.startswith("RESULT_JSON:")][-1].split(":", 1)[1])
    assert res["ok"] and Path(res["video"]).exists() and Path(res["contact_sheet"]).exists()
    info = media.probe(Path(res["video"]))
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert "Voz elegida" in r.stdout and "FASE 5" in r.stdout and "Aceptado" in r.stdout


def test_cli_doctor_runs():
    import subprocess, sys
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "app.cli", "doctor"], cwd=root, capture_output=True, text=True, timeout=60)
    assert "ffmpeg" in r.stdout


def test_dubvoice_retries_connection_reset(monkeypatch, tmp_path):
    import importlib
    importlib.reload(dubvoice)
    monkeypatch.setenv("DUBVOICE_API_KEY", "sk_test")
    img = tmp_path / "a.jpg"; img.write_bytes(_jpg())
    n = {"post": 0}

    class R:
        status_code = 200
        text = "x"
        def __init__(self, j): self._j = j
        def json(self): return self._j

    def fake(method, url, **kw):
        if method == "POST":
            n["post"] += 1
            if n["post"] < 3:
                raise RuntimeError("Sin conexion con https://www.dubvoice.ai/api/v1/video: [Errno 54] Connection reset by peer")
            return R({"task_id": "t"})
        return R({"status": "completed", "result": "https://x/v.mp4"})

    monkeypatch.setattr(dubvoice, "request", fake)
    monkeypatch.setattr(dubvoice, "download", lambda u: b"V")
    monkeypatch.setattr(dubvoice.time, "sleep", lambda s: None)
    dubvoice._poll_cache.clear()
    assert dubvoice.veo("p", img)[1] == b"V" and n["post"] == 3


def test_supervisor_retry_goes_to_google_after_dubvoice_failure(client, monkeypatch):
    """Primario DubVoice; tras dos timeouts (con job_id) el motor pasa al fallback Google Veo directo."""
    from app.phases import supervisor
    from app import store
    from app.services.errors import ErrorType, F5Error
    _fast_f5(monkeypatch)
    pid = _setup_sup_project(client, n=1)
    used = []

    def dub_fail(prompt, image_path, **k):
        used.append("dubvoice")
        raise F5Error(ErrorType.PROVIDER_TIMEOUT, "DubVoice (video) no termino en 10 min", provider="dubvoice", job_id=f"j{len(used)}")

    def google_ok(prompt, image_path, model="x", aspect="9:16", duration=8, resolution="720p", progress=None, timeout=0, cancel=None):
        used.append("google"); return "g", fake_video(prompt)

    monkeypatch.setattr(dubvoice, "veo", dub_fail)
    monkeypatch.setattr(google_veo, "veo", google_ok)
    monkeypatch.setattr(claude, "ask_json", lambda content, **k: {"visual_ok": True, "reasons": [], "prompt_fix": ""})
    monkeypatch.setattr(stt, "transcribe", lambda audio, settings=None, language_code=None: {
        "text": "ginger tea can calm your stomach", "words": []})
    supervisor.start(pid, lambda msg=None, progress=None: None)
    c = store.get(pid)["scenes"][0]["clips"][0]
    assert used == ["dubvoice", "dubvoice", "google"] and c["provider_used"] == "google" and c["verified"] and c["f5"]["state"] == "ACCEPTED"

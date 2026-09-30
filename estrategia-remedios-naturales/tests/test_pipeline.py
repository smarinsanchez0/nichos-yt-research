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

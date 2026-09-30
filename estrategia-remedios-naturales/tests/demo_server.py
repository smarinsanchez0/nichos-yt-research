"""Servidor de DEMO con servicios simulados (no gasta creditos): python tests/demo_server.py
Sirve para probar la interfaz completa sin API keys. Puerto 8099."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ERN_DATA_DIR", tempfile.mkdtemp(prefix="ern-demo-"))

import test_pipeline as T  # noqa: E402
from app import media  # noqa: E402
from app.services import claude, dubvoice, eleven, gemini, kie, stt  # noqa: E402

for k in ("ELEVENLABS_API_KEY", "KIE_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[k] = "demo"
claude.ask_json = T.fake_ask_json
stt.transcribe = T.fake_transcribe
eleven.list_voices = lambda: [
    {"voice_id": "v1", "name": "Ana", "category": "premade", "gender": "female", "age": "young", "accent": "american",
     "descriptive": "warm", "use_case": "social media", "preview_url": None},
    {"voice_id": "v2", "name": "Bob", "category": "premade", "gender": "male", "age": "middle aged", "accent": "american",
     "descriptive": "warm", "use_case": "conversational", "preview_url": None}]
dubvoice.list_voices = lambda gender=None, language='en', n=40: eleven.list_voices()
kie.upload_file = lambda p, mime='audio/mpeg', folder='': 'https://example.com/a.mp3'
dubvoice.voice_change = lambda url, vid, progress=None: T._mp3(8)
gemini.generate_image = lambda prompt, refs, model, aspect="9:16": T._jpg()
kie.upload_image = lambda p: "https://example.com/x.jpg"
kie.veo_generate = lambda prompt, url, model="veo3_fast", aspect="9:16", progress=None, timeout=0: ("t", T.fake_video(prompt))

if __name__ == "__main__":
    import uvicorn
    from app.main import app
    src = T.TMP / "demo_source.mp4"
    T.make_source(src)
    print("Video de prueba para subir:", src)
    uvicorn.run(app, host="127.0.0.1", port=8099)

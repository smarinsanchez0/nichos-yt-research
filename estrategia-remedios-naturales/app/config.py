"""Configuracion: importa las API keys desde ~/.zshrc (y compañia) sin exponerlas."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ERN_DATA_DIR", ROOT / "data"))
PROJECTS_DIR = DATA_DIR / "projects"
FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

SHELL_FILES = [
    "~/.zshrc", "~/.zshenv", "~/.zprofile", "~/.zlogin",
    "~/.bashrc", "~/.bash_profile", "~/.profile",
]

# servicio -> nombres de variable aceptados (en orden de preferencia)
KEY_ALIASES: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_KEY"],
    "elevenlabs": ["ELEVENLABS_API_KEY", "ELEVEN_LABS_API_KEY", "ELEVENLABS_KEY",
                   "ELEVEN_API_KEY", "XI_API_KEY"],
    "kie": ["KIE_API_KEY", "KIEAI_API_KEY", "KIE_AI_API_KEY", "KIE_KEY"],
    "google": ["GOOGLE_AI_STUDIO_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
               "GOOGLE_AI_API_KEY", "GOOGLE_GENAI_API_KEY"],
    "pexels": ["PEXELS_API_KEY", "PEXELS_KEY"],
    "dubvoice": ["DUBVOICE_API_KEY", "DUBVOICE_KEY", "DUB_VOICE_API_KEY"],
}
# palabras que identifican al servicio si el nombre de la variable es distinto
FUZZY_TOKENS = {
    "anthropic": ["ANTHROPIC"],
    "elevenlabs": ["ELEVEN", "XI_"],
    "kie": ["KIE"],
    "google": ["GEMINI", "GOOGLE", "AISTUDIO", "AI_STUDIO"],
    "pexels": ["PEXELS"],
    "dubvoice": ["DUBVOICE", "DUB_VOICE"],
}

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _clean_value(raw: str) -> str:
    raw = raw.strip()
    if raw and raw[0] in "\"'":
        q = raw[0]
        end = raw.find(q, 1)
        return raw[1:end] if end != -1 else raw[1:]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_shell_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            val = _clean_value(m.group(2))
            if val and "$(" not in val and "`" not in val:
                out[m.group(1)] = val
    return out


_loaded: dict[str, str] = {}


def load_env() -> dict[str, str]:
    """Lee .env local y los archivos de shell. El entorno real tiene prioridad."""
    _loaded.clear()
    for f in [ROOT / ".env", *[Path(p).expanduser() for p in SHELL_FILES]]:
        for k, v in parse_shell_file(f).items():
            _loaded.setdefault(k, v)
    return _loaded


def _lookup(name: str) -> str | None:
    return os.environ.get(name) or _loaded.get(name)


def _fuzzy(service: str) -> tuple[str, str] | None:
    """Variable cuyo nombre mencione el servicio y termine en KEY (evita falsos positivos)."""
    pool = {**_loaded, **os.environ}
    for name, v in pool.items():
        up = name.upper()
        if v and up.endswith("KEY") and any(t in up for t in FUZZY_TOKENS[service]) \
                and not re.search(r"SESSION|INGRESS|FILE|PATH|URL|PUBLIC", up):
            return name, v
    return None


def env(name: str) -> str | None:
    return _lookup(name)


def get_key(service: str) -> str | None:
    for name in KEY_ALIASES[service]:
        v = _lookup(name)
        if v:
            return v
    f = _fuzzy(service)
    return f[1] if f else None


def key_source(service: str) -> str | None:
    for name in KEY_ALIASES[service]:
        if _lookup(name):
            return name
    f = _fuzzy(service)
    return f[0] if f else None


def require_key(service: str) -> str:
    k = get_key(service)
    if not k:
        names = " / ".join(KEY_ALIASES[service][:2])
        raise RuntimeError(
            f"Falta la API key de {service}. Agrega `export {KEY_ALIASES[service][0]}=...` "
            f"en ~/.zshrc (o {names}) y reinicia la app."
        )
    return k


def mask(v: str | None) -> str | None:
    if not v:
        return None
    return "…" + v[-4:] if len(v) > 8 else "…"


_ffmpeg_cache: list = []


def _runs(path: str) -> bool:
    import subprocess
    try:
        return subprocess.run([path, "-version"], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ffmpeg_path() -> str | None:
    """ffmpeg que REALMENTE funciona: el del sistema, Homebrew o cualquiera de los binarios de imageio-ffmpeg."""
    if _ffmpeg_cache:
        return _ffmpeg_cache[0]
    cands = [shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    try:
        import glob
        import imageio_ffmpeg
        try:
            cands.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:  # noqa: BLE001
            pass
        bindir = Path(imageio_ffmpeg.__file__).parent / "binaries"
        cands += sorted(glob.glob(str(bindir / "ffmpeg*")))     # p. ej. la version arm64 aunque Python crea que es x86_64
    except ImportError:
        pass
    for c in cands:
        if c and Path(c).is_file() and _runs(c):
            _ffmpeg_cache.append(c)
            return c
    return None


def status() -> dict:
    services = {}
    for s in KEY_ALIASES:
        k = get_key(s)
        services[s] = {"ok": bool(k), "var": key_source(s), "hint": mask(k)}
    return {"keys": services, "ffmpeg": bool(ffmpeg_path())}


load_env()

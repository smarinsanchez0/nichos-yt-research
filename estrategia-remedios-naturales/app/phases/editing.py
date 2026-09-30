"""FASE 6: montaje, recorte de silencios, subtitulos Poppins (blanco/amarillo con trazo negro) y export."""
from __future__ import annotations

import re

from .. import media, store
from ..config import FONTS_DIR
from ..services import claude, stt
from .common import abs_path, norm

FALLBACK_KEY = {"free", "secret", "natural", "pain", "never", "stop", "only", "hidden", "doctors", "toxic", "cure",
                "remedy", "remedies", "fast", "today", "now", "proven", "recipe", "recipes", "book"}


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def pick_keywords(text: str, model: str) -> set[str]:
    try:
        data = claude.ask_json(
            "Del siguiente guion de un Reel de venta (ingles), elige las PALABRAS CLAVE que conviene resaltar en amarillo en los "
            "subtitulos: beneficios, ingredientes/remedios, cifras, emociones fuertes, llamada a la accion. Maximo 15% de las palabras. "
            'Devuelve {"keywords":["palabra", ...]} en minusculas, palabras sueltas.\n\n' + text,
            system="Eres editor de video experto en retencion en Instagram Reels.", model=model, max_tokens=1500)
        return {norm(k) for k in data.get("keywords", []) if norm(k)}
    except Exception:  # noqa: BLE001
        return set()


def chunk_for_subs(words: list[dict], per: int, max_chars: int = 20) -> list[list[dict]]:
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        chars = sum(len(x["text"]) + 1 for x in cur)
        brk = (len(cur) >= per or re.search(r"[.!?,;:…]$", w["text"]) or nxt is None
               or (nxt["start"] - w["end"]) > 0.45 or chars + len(nxt["text"]) > max_chars)
        if brk:
            chunks.append(cur)
            cur = []
    return chunks


def build_ass(words: list[dict], keywords: set[str], st: dict) -> str:
    size, mv = int(st["sub_font_size"]), int(st["sub_margin_v"])
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Poppins,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{max(6, size // 11)},0,2,70,70,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks = chunk_for_subs(words, int(st["sub_words_per_chunk"]))
    lines = []
    for n, ch in enumerate(chunks):
        start = ch[0]["start"]
        nxt = chunks[n + 1][0]["start"] if n + 1 < len(chunks) else None
        end = ch[-1]["end"] + 0.18
        if nxt is not None:
            end = min(end, nxt)
        end = max(end, start + 0.15)
        parts = []
        for w in ch:
            t = w["text"].replace("{", "(").replace("}", ")").replace("\\", "")
            t = t.upper() if st.get("sub_uppercase", True) else t
            if norm(w["text"]) in keywords:
                parts.append(r"{\c&H00FFFF&}" + t + r"{\c&HFFFFFF&}")
            else:
                parts.append(t)
        pop = r"{\fscx88\fscy88\t(0,90,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Main,,0,0,0,,{pop}{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


def overlap(expected: str, got: str) -> float:
    a = [norm(w) for w in expected.split() if norm(w)]
    b = {norm(w) for w in got.split() if norm(w)}
    return sum(1 for w in a if w in b) / len(a) if a else 1.0


def run(pid: str, prog) -> None:
    p = store.get(pid)
    st = p["settings"]
    ordered = [(si, ci, c) for si, s in enumerate(p["scenes"]) for ci, c in enumerate(s.get("clips", []))]
    if not ordered:
        raise RuntimeError("No hay clips. Completa las Fases 4 y 5.")
    missing = [f"E{si + 1}/C{ci + 1}" for si, ci, c in ordered if c.get("status") != "done" or not c.get("file")]
    if missing:
        raise RuntimeError("Faltan clips por generar: " + ", ".join(missing[:12]))
    work = store.path(pid, "final", ".keep").parent
    parts, silent_total, dropped = [], 0.0, 0.0
    for n, (si, ci, c) in enumerate(ordered):
        prog(f"Recortando silencios y normalizando clip {n + 1}/{len(ordered)}…", 0.05 + 0.5 * n / len(ordered))
        src = abs_path(pid, c["file"])
        info = media.probe(src)
        dst = work / f"part_{n:03d}.mp4"
        segs = None
        if c.get("dialogue") and info["has_audio"]:
            segs = media.speech_segments(src, info["duration"]) or None
        if segs is None:
            segs = [(0.0, min(info["duration"], float(c.get("target") or 8.0)))]
        # sin audio: se agrega pista muda para poder concatenar
        if not info["has_audio"]:
            tmp = work / f"tmp_{n:03d}.mp4"
            media.trim_video(src, tmp, info["duration"], has_audio=False)
            src = tmp
        out_dur = media.normalize_clip(src, dst, segs, has_audio=True)
        dropped += max(info["duration"] - out_dur, 0.0)
        parts.append(dst)
    prog("Uniendo escenas en orden…", 0.6)
    joined = work / "joined.mp4"
    media.concat(parts, joined)

    prog("Transcribiendo el audio final para los subtitulos exactos…", 0.7)
    audio = media.extract_audio(joined, work / "final_audio.mp3")
    tr = stt.transcribe(audio, st, language_code="en")
    expected = " ".join(c.get("dialogue", "") for _, _, c in ordered)
    ratio = overlap(expected, tr["text"])
    prog("Eligiendo palabras clave…", 0.78)
    keywords = pick_keywords(tr["text"], st["claude_model"])
    if not keywords:
        keywords = {norm(w["text"]) for w in tr["words"] if re.search(r"\d", w["text"]) or norm(w["text"]) in FALLBACK_KEY}
    ass = work / "subs.ass"
    ass.write_text(build_ass(tr["words"], keywords, st))
    prog("Quemando subtitulos y masterizando audio…", 0.85)
    out = work / "reel_final.mp4"
    media.burn_ass(joined, ass, out)
    info = media.probe(out)
    with store.edit(pid) as q:
        q["final"] = {"file": store.rel(pid, out), "subs": store.rel(pid, ass), "duration": info["duration"],
                      "silence_removed": round(dropped, 1), "script_match": round(ratio, 2),
                      "keywords": sorted(keywords), "font_dir": str(FONTS_DIR),
                      "warning": None if ratio >= 0.75 else
                      f"El audio final coincide solo un {int(ratio * 100)}% con el guion: revisa los clips que cambiaron el dialogo."}

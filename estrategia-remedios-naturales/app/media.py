"""Utilidades ffmpeg: sonda, escenas, frames, recorte de silencios, subtitulos."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import FONTS_DIR, ffmpeg_path


def _ff() -> str:
    p = ffmpeg_path()
    if not p:
        raise RuntimeError("ffmpeg no encontrado. Instala con `brew install ffmpeg` o `pip install imageio-ffmpeg`.")
    return p


def run(args: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([_ff(), "-hide_banner", "-y", *args], capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        raise RuntimeError("ffmpeg fallo: " + r.stderr.strip()[-1500:])
    return r


def probe(path: Path) -> dict:
    r = subprocess.run([_ff(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    err = r.stderr
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w = h = 0
    fps = 30.0
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    m = re.search(r"([\d.]+)\s*fps", err)
    if m:
        fps = float(m.group(1))
    return {"duration": dur, "width": w, "height": h, "fps": fps,
            "has_audio": bool(re.search(r"Stream #.*Audio:", err)),
            "has_video": bool(re.search(r"Stream #.*Video:", err))}


def extract_audio(video: Path, out: Path) -> Path:
    run(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)])
    return out


def detect_cuts(video: Path, threshold: float) -> list[float]:
    r = run(["-i", str(video), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"], check=False)
    return sorted({round(float(t), 3) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)})


def detect_scenes(video: Path, duration: float, threshold: float = 0.3,
                  min_len: float = 0.8, max_len: float = 9.0, max_scenes: int = 40) -> list[dict]:
    """Devuelve escenas [{start,end}]. Sube el umbral si hay demasiadas; parte las muy largas."""
    th = threshold
    for _ in range(6):
        cuts = detect_cuts(video, th)
        bounds = [0.0]
        for c in cuts:
            if c - bounds[-1] >= min_len and duration - c >= min_len:
                bounds.append(c)
        if len(bounds) <= max_scenes or th >= 0.8:
            break
        th += 0.1
    bounds.append(duration)
    scenes = []
    for a, b in zip(bounds, bounds[1:]):
        length = b - a
        parts = max(1, int(-(-length // max_len))) if max_len else 1
        step = length / parts
        for i in range(parts):
            scenes.append({"start": round(a + i * step, 3), "end": round(a + (i + 1) * step, 3)})
    return scenes


def extract_frame(video: Path, t: float, out: Path, width: int = 720) -> Path:
    run(["-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2",
         "-q:v", "3", str(out)])
    if not out.exists():
        raise RuntimeError(f"No se pudo extraer el frame en t={t:.2f}s")
    return out


def _silences(path: Path, noise_db: float, min_sil: float) -> list[tuple[float, float]]:
    r = run(["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_sil}",
             "-f", "null", "-"], check=False)
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append((max(s, 0.0), e if e is not None else float("inf")))
    return out


def speech_segments(path: Path, duration: float, noise_db: float = -34, min_sil: float = 0.35,
                    pad: float = 0.10, min_keep: float = 0.15) -> list[tuple[float, float]]:
    """Intervalos con audio (complemento de los silencios) con un pequeño margen."""
    sil = _silences(path, noise_db, min_sil)
    segs, cur = [], 0.0
    for s, e in sil:
        if s - cur > 0:
            segs.append((cur, min(s, duration)))
        cur = e
    if cur < duration:
        segs.append((cur, duration))
    segs = [(max(a - pad, 0.0), min(b + pad, duration)) for a, b in segs if b - a >= min_keep]
    merged: list[list[float]] = []
    for a, b in segs:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def normalize_clip(src: Path, dst: Path, segments: list[tuple[float, float]] | None,
                   has_audio: bool, width: int = 1080, height: int = 1920, fps: int = 30) -> float:
    """Recorta segmentos (si se dan), normaliza a 1080x1920/30fps y devuelve la duracion final."""
    vf_tail = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
               f"fps={fps},format=yuv420p,setsar=1")
    if not segments:
        args = ["-i", str(src), "-vf", vf_tail]
        args += (["-af", "aresample=48000"] if has_audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"])
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(dst)]
        run(args)
        return probe(dst)["duration"]
    parts, labels = [], []
    for i, (a, b) in enumerate(segments):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        fade = min(0.02, (b - a) / 4)
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d={fade:.3f},afade=t=out:st={max(b - a - fade, 0):.3f}:d={fade:.3f}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    n = len(segments)
    parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]{vf_tail}[vout]")
    parts.append("[ac]aresample=48000[aout]")
    run(["-i", str(src), "-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(dst)])
    return probe(dst)["duration"]


def trim_video(src: Path, dst: Path, seconds: float, has_audio: bool = True) -> None:
    """Recorta a `seconds`. Si no hay audio, agrega pista silenciosa para poder concatenar."""
    args = ["-i", str(src), "-t", f"{seconds:.3f}"]
    if not has_audio:
        args = ["-i", str(src), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.3f}"]
    run(args + ["-c:v", "libx264", "-crf", "18", "-c:a", "aac", str(dst)])


def concat(files: list[Path], out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(f).resolve()}'\n" for f in files))
    run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])


def mux_audio(video: Path, audio: Path, out: Path) -> None:
    run(["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])


def _esc_filter_path(p: str) -> str:
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def burn_ass(video: Path, ass: Path, out: Path, loudnorm: bool = True) -> None:
    vf = f"ass={ass.name}:fontsdir={_esc_filter_path(str(FONTS_DIR))}"
    args = ["-i", str(video), "-vf", vf]
    if loudnorm:
        args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    run(args, cwd=ass.parent)

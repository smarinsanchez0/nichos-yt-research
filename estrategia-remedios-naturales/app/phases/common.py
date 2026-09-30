from __future__ import annotations

import re
from pathlib import Path

from .. import store


def abs_path(pid: str, relpath: str) -> Path:
    return store.pdir(pid) / relpath


def assign_words(words: list[dict], scenes: list[dict]) -> list[list[dict]]:
    """Reparte las palabras por escena segun el punto medio temporal de cada palabra."""
    out: list[list[dict]] = [[] for _ in scenes]
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        idx = len(scenes) - 1
        for i, s in enumerate(scenes):
            if s["start"] <= mid < s["end"]:
                idx = i
                break
        out[idx].append(w)
    return out


def make_segments(words: list[dict], max_words: int = 28, gap: float = 0.7) -> list[dict]:
    """Agrupa palabras en frases (por puntuacion o pausas) para traducir con contexto."""
    segs, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        end_sent = bool(re.search(r"[.!?…]$", w["text"]))
        if end_sent or nxt is None or nxt["start"] - w["end"] > gap or len(cur) >= max_words:
            segs.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                         "en": " ".join(x["text"] for x in cur)})
            cur = []
    return segs


def norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())

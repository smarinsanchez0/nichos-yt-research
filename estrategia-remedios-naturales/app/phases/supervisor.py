"""SUPERVISOR CLAUDE de la Fase 5: con un clic lanza todos los clips, los vigila en tiempo real, audita cada resultado
(audio, texto hablado, fotogramas) y decide: aceptar, reintentar (con prompt/modelo/duracion distintos), cancelar lo que se
atasca o rendirse. Limites duros protegen el gasto. Si Claude no responde, sigue en piloto automatico."""
from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import media, store
from ..services import claude, dubvoice, stt
from . import editing, videos
from .common import abs_path

MAX_ATTEMPTS = 3            # intentos por clip
STALL_SECONDS = 12 * 60     # cancelacion dura de un clip atascado
DEADLINE_SECONDS = 75 * 60  # tiempo maximo de toda la corrida
MAX_TURNS = 90
MODELS = {"veo-3.1-fast": "7.500 cr, 8 s", "veo-3.1-lite": "9.100 cr, 8 s", "veo-3.1": "17.000 cr, 8 s, alta calidad",
          "omniflash": "4.688–9.375 cr segun 4/6/8/10 s"}

_active: dict[str, "Supervisor"] = {}

SYSTEM = """Eres el SUPERVISOR de produccion de un equipo que replica Reels (9:16) con un avatar de IA. Diriges la generacion de clips de video
(imagen inicial + prompt + dialogo -> clip con voz) con DubVoice/Veo 3.1. Un motor ya lanza y ejecuta los clips en paralelo (max 3);
tu trabajo es AUDITAR y DECIDIR en tiempo real, sin gastar de mas.

Recibes cada turno: el estado de todos los clips, los eventos nuevos, tu bitacora reciente y, para clips terminados pendientes de
revision, 3 fotogramas + la imagen aprobada de referencia + la auditoria tecnica (duracion, audio, texto transcrito y coincidencia con el dialogo).

Criterios para ACEPTAR un clip: (1) la persona de los fotogramas es el avatar de la imagen de referencia (misma cara, ropa y ambiente, sin
deformaciones graves); (2) hace la accion pedida; (3) hay audio con voz (speech_seconds > 1) y la coincidencia con el dialogo (match) es >= 0.6
(si no hay transcripcion, juzga por el audio); (4) no hay subtitulos ni texto superpuesto; (5) la duracion cubre el dialogo (no se corta a media frase).
Si falla: RECHAZA y ordena reintentar. Ideas: mejorar el prompt (mas corto/simple, reforzar la accion o repetir el dialogo exacto), cambiar de
modelo tras 2 fallos (veo-3.1-lite u omniflash como alternativa; veo-3.1 solo si es imprescindible por su costo), o subir la duracion si el dialogo se corta.
Errores de politica/contenido: reescribe el prompt suavizandolo sin cambiar el dialogo. Errores de red/servicio: reintenta. Clip atascado (>6 min): cancelalo y reintenta.
Tras 3 intentos fallidos usa give_up con la razon. Nunca cambies el dialogo salvo para acortarlo si no cabe.

Responde UNICAMENTE con JSON: {"analysis": "1-3 frases en español sobre lo que ves y decides", "actions": [ ... ]}
Acciones (lista, se ejecutan en orden):
  {"do":"accept","scene":N,"clip":K}
  {"do":"retry","scene":N,"clip":K,"reason":"...","prompt":"(opcional) nuevo prompt de video completo","model":"(opcional)","duration":(opcional segundos)}
  {"do":"cancel","scene":N,"clip":K}                 (corta un clip atascado; luego usa retry)
  {"do":"retry_voice","scene":N,"clip":K}            (solo repite el cambio de voz si el aviso lo pide)
  {"do":"give_up","scene":N,"clip":K,"reason":"..."}
  {"do":"finish","summary":"resumen en español para el usuario"}   (solo cuando no quede nada pendiente)
Numeracion: escenas y clips desde 1. Si no hay nada que decidir, devuelve "actions": []."""


class Supervisor:
    def __init__(self, pid: str, prog):
        self.pid, self.prog = pid, prog
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.cancels: dict[tuple, threading.Event] = {}
        self.attempts: dict[tuple, int] = {}
        self.state: dict[tuple, str] = {}        # queued | running | audited | accepted | gave_up | failed
        self.started: dict[tuple, float] = {}
        self.notes: dict[tuple, str] = {}
        self.last_error: dict[tuple, str] = {}
        self.pending_review: set[tuple] = set()
        self.stop_flag = threading.Event()
        self.t0 = time.time()
        self.spent = 0
        p = store.get(pid)
        self.keys = [(s["idx"], c["idx"]) for s in p["scenes"] for c in s["clips"]]
        est = videos.estimate(p)
        self.budget = int(max(est["total_credits"], 1) * 2.2) + 20000
        self.claude_fail = 0
        self.audited: dict = {}
        self.gen: dict[tuple, int] = {}
        self.review_turns: dict[tuple, int] = {}
        self._summary = ""

    # ------------------------------------------------------------ bitacora
    def log(self, msg: str) -> None:
        with store.edit(self.pid) as p:
            j = p["jobs"].setdefault("supervisor", {})
            lg = j.setdefault("log", [])
            lg.append({"t": time.strftime("%H:%M:%S"), "msg": msg[:600]})
            del lg[:-80]
        self.prog(msg[:160])

    def clip_label(self, k) -> str:
        return f"escena {k[0] + 1} clip {k[1] + 1}"

    # ------------------------------------------------------------ motor
    def _work(self, key, overrides, gen):
        si, ci = key
        cancel = self.cancels[key] = threading.Event()
        self.started[key] = time.time()
        self.state[key] = "running"
        model = overrides.get("model") or store.get(self.pid)["settings"]["dubvoice_video_model"]
        cost = dubvoice.credits_for(model, overrides.get("duration") or self._clip(key).get("target") or 8)
        self.spent += cost
        try:
            videos.render_clip(self.pid, si, ci, prog=lambda m: self.notes.__setitem__(key, m), cancel=cancel, overrides=overrides)
            if self.gen.get(key) == gen:
                self.state[key] = "done"
                self.events.put((key, "done", ""))
        except Exception as e:  # noqa: BLE001
            self.spent -= cost                      # DubVoice reembolsa los fallos
            if self.gen.get(key) == gen:            # un intento cancelado/antiguo no pisa al nuevo
                self.state[key] = "failed"
                self.last_error[key] = str(e)[:400]
                self.events.put((key, "failed", str(e)[:400]))

    def launch(self, key, overrides=None):
        if self.attempts.get(key, 0) >= MAX_ATTEMPTS:
            return f"{self.clip_label(key)}: ya uso {MAX_ATTEMPTS} intentos"
        model = (overrides or {}).get("model")
        if model and model not in MODELS:
            return f"modelo desconocido {model}"
        est = dubvoice.credits_for(model or store.get(self.pid)["settings"]["dubvoice_video_model"], 8)
        if self.spent + est > self.budget:
            self.state[key] = "gave_up"
            return f"presupuesto agotado ({self.spent}/{self.budget} creditos)"
        self.attempts[key] = self.attempts.get(key, 0) + 1
        with store.edit(self.pid) as q:
            q["scenes"][key[0]]["clips"][key[1]]["attempts"] = self.attempts[key]
        self.gen[key] = self.gen.get(key, 0) + 1
        self.state[key] = "queued"
        self.pool.submit(self._work, key, overrides or {}, self.gen[key])
        return None

    def _clip(self, key) -> dict:
        return store.get(self.pid)["scenes"][key[0]]["clips"][key[1]]

    # ------------------------------------------------------------ auditoria
    def audit(self, key) -> dict:
        p = store.get(self.pid)
        st = p["settings"]
        c = p["scenes"][key[0]]["clips"][key[1]]
        f = abs_path(self.pid, c["file"])
        info = media.probe(f)
        a = {"duration": round(info["duration"], 1), "has_audio": info["has_audio"], "asked_seconds": c.get("asked_seconds"),
             "warning": c.get("warning")}
        if info["has_audio"]:
            segs = media.speech_segments(f, info["duration"])
            a["speech_seconds"] = round(sum(b - x for x, b in segs), 1)
            a["ends_with_speech"] = bool(segs) and segs[-1][1] >= info["duration"] - 0.15
            if c.get("dialogue"):
                try:
                    wav = media.extract_audio(f, store.path(self.pid, "work", "audit", f"s{key[0]}_c{key[1]}.mp3"))
                    tr = stt.transcribe(wav, st, language_code=st.get("output_language", "es"))
                    a["transcript"] = tr["text"][:300]
                    a["match"] = round(editing.overlap(c["dialogue"], tr["text"]), 2)
                except Exception as e:  # noqa: BLE001
                    a["transcript_error"] = str(e)[:120]
        frames = []
        for i, frac in enumerate((0.15, 0.5, 0.85)):
            frames.append(store.rel(self.pid, media.extract_frame(
                f, info["duration"] * frac, store.path(self.pid, "work", "audit", f"s{key[0]}_c{key[1]}_f{i}.jpg"), width=384)))
        a["frames"] = frames
        with store.edit(self.pid) as q:
            q["scenes"][key[0]]["clips"][key[1]]["audit"] = {k: v for k, v in a.items() if k != "frames"}
        return a

    def basic_ok(self, a: dict, key) -> bool:
        c = self._clip(key)
        if not a["has_audio"] or a["duration"] < 1.5:
            return False
        if c.get("dialogue"):
            if a.get("speech_seconds", 0) < 1.0:
                return False
            if "match" in a and a["match"] < 0.5:
                return False
        return True

    # ------------------------------------------------------------ estado para Claude
    def snapshot(self) -> list[dict]:
        p = store.get(self.pid)
        rows = []
        for k in self.keys:
            c = p["scenes"][k[0]]["clips"][k[1]]
            row = {"scene": k[0] + 1, "clip": k[1] + 1, "state": self.state.get(k, "queued"), "attempts": self.attempts.get(k, 0),
                   "dialogue": (c.get("dialogue") or "")[:90], "target_s": c.get("target"), "model": c.get("model_used") or p["settings"]["dubvoice_video_model"]}
            if self.state.get(k) == "running":
                row["running_min"] = round((time.time() - self.started.get(k, time.time())) / 60, 1)
                row["last_status"] = self.notes.get(k, "")[-90:]
            if self.last_error.get(k) and self.state.get(k) in ("failed", "gave_up"):
                row["error"] = self.last_error[k]
            rows.append(row)
        return rows

    def decide(self, events: list[str], reviews: list[tuple]) -> dict | None:
        p = store.get(self.pid)
        content: list[dict] = []
        for key, a in reviews[:2]:
            c = self._clip(key)
            content.append({"type": "text", "text": f"--- REVISION escena {key[0] + 1} clip {key[1] + 1} | dialogo esperado: \"{c.get('dialogue')}\" | "
                            f"accion: {c.get('action_en') or c.get('action_es')} | auditoria: {json.dumps({k: v for k, v in a.items() if k != 'frames'}, ensure_ascii=False)}"})
            content.append({"type": "text", "text": "Imagen aprobada de referencia (el avatar y el ambiente esperados):"})
            content.append(claude.image_block(abs_path(self.pid, p["scenes"][key[0]]["image"]["file"]), 384))
            content.append({"type": "text", "text": "Fotogramas del clip generado (inicio, medio, final):"})
            for fr in a["frames"]:
                content.append(claude.image_block(abs_path(self.pid, fr), 384))
        elapsed = int(time.time() - self.t0)
        budget_txt = f"creditos gastados aprox {self.spent} de {self.budget} permitidos"
        content.append({"type": "text", "text": (
            f"ESTADO (t={elapsed // 60} min, {budget_txt}; max {MAX_ATTEMPTS} intentos por clip):\n{json.dumps(self.snapshot(), ensure_ascii=False)}\n\n"
            f"EVENTOS NUEVOS: {json.dumps(events, ensure_ascii=False)}\n"
            f"CLIPS PENDIENTES DE TU REVISION (te muestro sus fotogramas arriba): {[f'E{k[0]+1}C{k[1]+1}' for k, _ in reviews]}\n"
            f"BITACORA RECIENTE: {json.dumps(self._recent_log(), ensure_ascii=False)}\n"
            f"Modelos disponibles: {json.dumps(MODELS, ensure_ascii=False)}. Decide ahora.")})
        try:
            data = claude.ask_json(content, system=SYSTEM, model=p["settings"]["claude_model"], max_tokens=1500)
            self.claude_fail = 0
            return data
        except Exception as e:  # noqa: BLE001
            self.claude_fail += 1
            self.log(f"⚠️ Claude no respondio ({str(e)[:120]}). {'Sigo en piloto automatico.' if self.claude_fail >= 3 else 'Reintento en el proximo ciclo.'}")
            return None

    def _recent_log(self) -> list[str]:
        j = store.get(self.pid)["jobs"].get("supervisor", {})
        return [f"{x['t']} {x['msg'][:160]}" for x in j.get("log", [])[-10:]]

    # ------------------------------------------------------------ ejecucion de acciones
    def apply(self, actions: list[dict]) -> bool:
        finished = False
        for a in actions or []:
            try:
                do = a.get("do")
                key = (int(a.get("scene", 0)) - 1, int(a.get("clip", 0)) - 1)
                if do != "finish" and key not in self.keys:
                    self.log(f"⚠️ Accion ignorada (clip inexistente): {a}")
                    continue
                if do == "accept":
                    self.state[key] = "accepted"
                    with store.edit(self.pid) as q:
                        q["scenes"][key[0]]["clips"][key[1]]["verified"] = True
                    self.pending_review.discard(key)
                    self.log(f"✅ Aceptado {self.clip_label(key)}")
                elif do == "retry":
                    ov = {k: a[k] for k in ("prompt", "model", "duration") if a.get(k)}
                    if "duration" in ov:
                        try:
                            ov["duration"] = float(ov["duration"])
                        except (TypeError, ValueError):
                            ov.pop("duration")
                    if self.state.get(key) == "running" and key in self.cancels:
                        self.cancels[key].set()
                    self.pending_review.discard(key)
                    err = self.launch(key, ov)
                    self.log(f"🔁 Reintento {self.clip_label(key)} (intento {self.attempts.get(key, 0)}): {a.get('reason', '')}"
                             + (f" — {', '.join(ov)} cambiado" if ov else "") + (f" ⛔ {err}" if err else ""))
                elif do == "cancel":
                    self.cancels.get(key, threading.Event()).set()
                    self.log(f"⛔ Cancelado {self.clip_label(key)}")
                elif do == "retry_voice":
                    w = videos.retry_voice(self.pid, key[0], key[1])
                    self.log(f"🎙️ Cambio de voz repetido en {self.clip_label(key)}: {w or 'ok'}")
                elif do == "give_up":
                    self.state[key] = "gave_up"
                    self.pending_review.discard(key)
                    self.log(f"🚫 Descartado {self.clip_label(key)}: {a.get('reason', '')}")
                elif do == "finish":
                    finished = True
                    self._summary = a.get("summary", "")
            except Exception as e:  # noqa: BLE001
                self.log(f"⚠️ No pude ejecutar {a}: {str(e)[:160]}")
        return finished

    # ------------------------------------------------------------ piloto automatico (sin Claude)
    def autopilot(self, reviews, failures) -> None:
        for key, a in reviews:
            if self.basic_ok(a, key):
                self.apply([{"do": "accept", "scene": key[0] + 1, "clip": key[1] + 1}])
            elif self.attempts.get(key, 0) < MAX_ATTEMPTS:
                self.apply([{"do": "retry", "scene": key[0] + 1, "clip": key[1] + 1, "reason": "auditoria basica fallida (autopiloto)"}])
            else:
                self.apply([{"do": "give_up", "scene": key[0] + 1, "clip": key[1] + 1, "reason": "auditoria basica fallida"}])
        for key in failures:
            if self.attempts.get(key, 0) < MAX_ATTEMPTS:
                self.apply([{"do": "retry", "scene": key[0] + 1, "clip": key[1] + 1, "reason": f"error: {self.last_error.get(key, '')[:80]} (autopiloto)"}])
            else:
                self.apply([{"do": "give_up", "scene": key[0] + 1, "clip": key[1] + 1, "reason": self.last_error.get(key, "")[:120]}])

    # ------------------------------------------------------------ bucle principal
    def run(self) -> None:
        p = store.get(self.pid)
        if p["settings"].get("unify_voice"):
            if not p["settings"].get("voice_id"):
                raise RuntimeError("Elige una voz (o desactiva 'unificar voz') antes de usar el supervisor.")
        todo = [k for k in self.keys if p["scenes"][k[0]]["clips"][k[1]].get("status") != "done" or p["scenes"][k[0]]["clips"][k[1]].get("stale")]
        for k in self.keys:
            if k not in todo:
                self.state[k] = "accepted"
        if not todo:
            self.log("Todos los clips ya estaban listos.")
            return
        self.log(f"🚀 Supervisor iniciado: {len(todo)} clips, presupuesto {self.budget:,} creditos, max {MAX_ATTEMPTS} intentos por clip.")
        for k in todo:
            self.launch(k)
        turns, last_call = 0, 0.0
        self._summary = ""
        while not self.stop_flag.is_set():
            if time.time() - self.t0 > DEADLINE_SECONDS:
                self.log("⏱️ Tiempo maximo de la corrida alcanzado; detengo lo pendiente.")
                for ev in self.cancels.values():
                    ev.set()
                break
            evs: list[str] = []
            try:
                key, kind, msg = self.events.get(timeout=20)
                while True:
                    self._on_event(key, kind, msg, evs)
                    key, kind, msg = self.events.get_nowait()
            except queue.Empty:
                pass
            # atascos duros
            for k, st_ in list(self.state.items()):
                if st_ == "running" and time.time() - self.started.get(k, time.time()) > STALL_SECONDS:
                    self.cancels[k].set()
                    evs.append(f"{self.clip_label(k)} superó {STALL_SECONDS // 60} min: cancelado por el motor")
            reviews = []
            for k in sorted(self.pending_review):
                if k not in self.audited:
                    self.audited[k] = self.audit(k)
                reviews.append((k, self.audited[k]))
            # un clip que Claude no logra decidir en 3 ciclos pasa al piloto automatico
            stuck = [(k, a) for k, a in reviews if self.review_turns.get(k, 0) >= 3]
            if stuck:
                self.autopilot(stuck, [])
                reviews = [(k, a) for k, a in reviews if k in self.pending_review]
            failures = [k for k in self.keys if self.state.get(k) == "failed"]
            running = [k for k in self.keys if self.state.get(k) in ("running", "queued")]
            slow = [k for k in running if self.state.get(k) == "running" and time.time() - self.started.get(k, 0) > 6 * 60]
            self.prog(f"{sum(1 for k in self.keys if self.state.get(k) == 'accepted')}/{len(self.keys)} aceptados · {len(running)} en curso · "
                      f"{len(failures)} con error", sum(1 for k in self.keys if self.state.get(k) in ('accepted', 'gave_up')) / len(self.keys))
            need_decision = bool(reviews or failures or slow or (time.time() - last_call > 180 and running))
            if need_decision and turns < MAX_TURNS:
                turns += 1
                last_call = time.time()
                for k, _ in reviews[:2]:
                    self.review_turns[k] = self.review_turns.get(k, 0) + 1
                data = self.decide(evs, reviews[:2]) if self.claude_fail < 3 else None
                if data is None:
                    self.autopilot(reviews[:2], failures)
                else:
                    if data.get("analysis"):
                        self.log("🧠 " + str(data["analysis"]))
                    self.apply(data.get("actions") or [])
                    # red de seguridad: lo que Claude no resolvio en este turno no se queda colgado
                    for k, a in reviews[:2]:
                        if k in self.pending_review and self.state.get(k) == "done":
                            self.log(f"ℹ️ Claude no decidio {self.clip_label(k)}; lo reviso otra vez en el proximo ciclo.")
                    for k in failures:
                        if self.state.get(k) == "failed" and turns > MAX_TURNS - 3:
                            self.autopilot([], [k])
            elif need_decision:
                self.autopilot(reviews, failures)
            unresolved = [k for k in self.keys if self.state.get(k) not in ("accepted", "gave_up")]
            if not unresolved:
                break
        ok = sum(1 for k in self.keys if self.state.get(k) == "accepted")
        bad = [self.clip_label(k) + (f" [{self.last_error[k][:110]}]" if self.last_error.get(k) else "")
               for k in self.keys if self.state.get(k) != "accepted"]
        self.log(f"🏁 Terminado: {ok}/{len(self.keys)} clips aceptados" + (f"; sin resolver: {', '.join(bad)}" if bad else "") +
                 (f". {self._summary}" if self._summary else "") + f" (≈{self.spent:,} creditos)")
        self.pool.shutdown(wait=False, cancel_futures=True)
        if bad and not self.stop_flag.is_set():
            raise RuntimeError("Clips sin resolver: " + "; ".join(bad) + ". Vuelve a pulsar el boton del supervisor: "
                               "solo reintenta esos clips (los demas quedan guardados).")

    def _on_event(self, key, kind, msg, evs: list[str]) -> None:
        if kind == "done":
            self.pending_review.add(key)
            self.audited.pop(key, None)
            self.state[key] = "done"
            evs.append(f"{self.clip_label(key)} generado")
            self.log(f"🎬 Generado {self.clip_label(key)} — auditando…")
        else:
            evs.append(f"{self.clip_label(key)} ERROR: {msg}")
            self.log(f"❌ {self.clip_label(key)}: {msg[:200]}")


def start(pid: str, prog) -> None:
    sup = Supervisor(pid, prog)
    _active[pid] = sup
    try:
        sup.run()
    finally:
        _active.pop(pid, None)


def stop(pid: str) -> None:
    sup = _active.get(pid)
    if sup:
        sup.stop_flag.set()
        for ev in sup.cancels.values():
            ev.set()

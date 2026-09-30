# REPORTE PASO 3 — Fase 5 (motor de trabajos de video)

Rama: `claude/ai-video-replication-app-wtfzso` · Sin merge a `main` · Sin llamadas a APIs reales · F1–F4, F6 y UI **sin cambios**.

## 0. Los tres ajustes obligatorios (implementados tal cual)

| Ajuste | Implementación | Test que lo prueba |
|---|---|---|
| 1. `F5_AMBIGUOUS_SUBMIT_RETRIES=0` por defecto | POST enviado + reset antes de recibir `job_id` → `NEEDS_REVIEW` (`AMBIGUOUS_SUBMIT`), `possible_duplicate=true`, `remote_unknown=true`, **cero reenvíos**. Un `2xx` sin `job_id` reconocible se trata igual (puede haber cobrado). | `test_ambiguous_submit_is_never_resent_automatically`, `test_decide_ambiguous_retry_is_opt_in_and_limits_are_enforced`, `test_dubvoice_strict_2xx_without_job_id_is_ambiguous_and_visible` |
| 2. `MAX_TOTAL_GENERATION_TIME=1200` (20 min) y `PROCESSING_TIMEOUT=720` (12 min) | Todos los valores viven en **un solo lugar** (`video_jobs.config()`): entorno / `~/.zshrc` > `data/f5_config.json` > defecto. Ningún número suelto en el código. | `test_configuration_is_central_and_editable_without_code`, `test_configuration_also_reads_exports_from_zshrc_style_files` |
| 3. El audio no regenera el video | Eliminada la regla `dialogue_cov < 0.5 → regenerar`. Video visualmente bueno ⇒ `visual_state=OK`, `ACCEPTED`. Audio/diálogo mal ⇒ `audio_state=NEEDS_FIX` (o `UNVERIFIED`), RAW conservado, **0 generaciones nuevas**. Solo `QUALITY_REJECTED` **visual** (o material más corto que el tramo) regenera. | tests A, B, C, D, `test_visual_rejection_is_the_only_thing_that_regenerates_video` |

## 1. Archivos creados
| Archivo | Líneas | Qué es |
|---|---|---|
| `app/services/errors.py` | 189 | Taxonomía tipada (`ErrorType`, `F5Error`, `Cancelled`), `classify()`, `sanitize()`/`scrub()` de secretos, `shape()` |
| `app/phases/video_jobs.py` | 1623 | Núcleo de F5: config central, máquina de estados, `clip.f5` + intentos, línea de tiempo, política (`decide`), `Scheduler` (slots + rate gate + ejecutores), `ClipDriver`, reconciliación, telemetría |
| `tests/test_f5_jobs.py` | 1302 | 64 tests nuevos (todos simulados) |
| `tests/conftest.py` | 5 | Un solo directorio de datos temporal para toda la suite |
| `tools/f5_report.py` | 96 | *(no estaba en el plan)* Reporte de solo lectura para anotar la prueba real |

## 2. Archivos modificados
`app/phases/supervisor.py` (reducido a QC + reescritura; API `start`/`stop` intacta) · `app/phases/videos.py` (`generate_raw`, `resume_raw`, `finish_clip`; `render_clip` heredado; `render_many` delega) · `app/services/dubvoice.py` (camino estricto, `resume`, `ContractRecorder`) · `app/services/google_veo.py` (camino estricto, `resume`) · `app/services/http.py` (`request_once`, `download`, `typed_error`) · `app/jobs.py` (3 líneas) · `app/autopilot.py` (llamada a F5) · `app/main.py` (rutas → planificador único, `/f5/summary`, `/f5/clip`) · `app/store.py` (2 líneas de comentario/valor por defecto obsoletos) · `app/demo.py` y `tests/test_pipeline.py` (fakes + 5 tests reescritos).

**No tocados:** `avatar.py`, `analysis.py`, `images.py`, `fragment.py`, `metaprompts.py`, `editing.py` (F6), `media.py`, `kie.py`, `app/static/*` (UI).

## 3. Líneas
`git diff --cached --stat`: **16 archivos, +4047 / −486** (incluye tests y el reporte de herramientas).

## 4. Arquitectura final de F5
```
UI / autopilot / CLI ──► supervisor.start | /videos/generate | /videos/{i}/{j}/regenerate
                                   │
                                   ▼
                        video_jobs.run_project()        (UNA sola entrada; no hay más pools)
                                   │  un ClipDriver por clip
   ┌───────────────────────────────┴───────────────────────────────────────────────┐
   │  SLOT DE GENERACIÓN  (MAX_CONCURRENT_VIDEO_JOBS)   ← solo SUBMIT → POLL → RAW  │
   │   RateGate por proveedor (VIDEO_REQUESTS_PER_MINUTE) + cooldown global en 429  │
   │   run_bounded() watchdog: ninguna llamada puede secuestrar un worker           │
   │   on_submit(job_id) ─► persiste el job_id en project.json AL INSTANTE          │
   └──────────────► RAW guardado (atómico) ─► SLOT LIBERADO ─────────────────────────┘
                                   │
                    ejecutor de post-proceso (fuera del slot)
                     1) QC visual (Claude, 1 llamada)   ─ rechaza ► RETRY_PENDING
                     2) voz / unify (acotado)           ─ falla ► audio NEEDS_FIX, RAW intacto
                     3) auditoría ffmpeg+Whisper        ─ falla ► audio UNVERIFIED, RAW intacto
                     4) ACCEPTED (visual)  + campos heredados (status/file/…)
```
* **Política determinista** (`decide`): por tipo de error; Claude no decide timeouts, rate limits, conexión, topes, fallback ni estado global.
* **Primario → fallback**: DubVoice `veo-3.1-fast` → (reintento inteligente) → Google Veo. `omniflash`/`meta`/imagen fija **fuera** de la política automática.
* **Contrato DubVoice**: `ContractRecorder` + puerta canario (1 solo job hasta verificar un ciclo completo; si no coincide se congela el proveedor).
* **Reapertura**: `recover_after_restart()` + `reconcile()` (job conocido → solo sondea/descarga; `SUBMITTING` sin `job_id` → `NEEDS_REVIEW`).

## 5. Máquina de estados implementada
Clip: `PENDING → SUBMITTED → PROCESSING → VALIDATING → ACCEPTED`; `RETRY_PENDING`; `FAILED` (solo si nunca se pagó nada: precondición/config); `NEEDS_REVIEW`. Tabla `video_jobs.TRANSITIONS` (transición inválida ⇒ `InvalidTransition`).
`NEEDS_REVIEW` solo sale por recuperación **gratis** (reanudar desde el RAW / reconciliar el job) o por acción explícita del usuario.
Espejo a campos heredados: `PENDING/RETRY_PENDING→pending`, `SUBMITTED/PROCESSING/VALIDATING→running`, `ACCEPTED→done`, `FAILED/NEEDS_REVIEW→error`.
Global: `RUNNING`, `COMPLETED`, `COMPLETED_WITH_WARNINGS` (≥1 clip en `NEEDS_REVIEW`/`FAILED`), `FAILED` (todos los clips `FAILED`, o excepción interna del motor).

## 6. Resultado de TODOS los tests
`pytest -q tests` → **86 passed** (corrida completa estable; ~77 s).
* 22 tests preexistentes: 17 sin tocar + 5 reescritos (ver §8).
* 64 tests nuevos en `tests/test_f5_jobs.py`.
* Ningún test llama a una API real (proveedores, Claude y Whisper simulados; solo sockets locales `127.0.0.1` para probar deadlines HTTP y ffmpeg real).

## 7. Tests nuevos y qué verifican
| Evidencia pedida | Test(s) |
|---|---|
| Un worker no puede quedar secuestrado | `test_hung_provider_call_cannot_hijack_a_worker` (proveedor colgado → watchdog, lote termina), `test_run_bounded_abandons_uninterruptible_calls_and_honors_cancel`, `test_http_request_once_and_download_respect_deadline_and_cancel` (servidor mudo / que gotea bytes; cancelación), B (voz colgada) |
| Un clip no bloquea el lote | `test_a_clip_that_hits_its_limit_goes_to_needs_review_and_the_batch_continues`, `test_11_accepted_plus_1_needs_review_ends_completed_with_warnings` |
| Un RAW pagado se conserva | A, B, C, D, `test_short_provider_clip_is_rejected…` (RAW rechazado sigue en disco), `test_interrupted_validation_resumes_from_the_saved_raw_for_free` |
| Audio no regenera video | A (audio incorrecto), B (voz timeout), C (Whisper falla), D (Claude QC falla → `NEEDS_REVIEW` y luego reanuda **desde el RAW con 0 POST nuevos**) |
| Clip 8 s para target 3 s se acepta | `test_8s_provider_clip_for_3s_target_is_visually_accepted` (`source 10→13`, `target_duration=3.0`, `provider_duration≈8`, 1 intento, sin warning) + `test_timeline_is_derived_from_the_original_scene_not_from_spanish_target` |
| ACCEPTED no se vuelve a pagar | F (`restart` con ACCEPTED ⇒ 0 POST y 0 llamadas a Claude), `test_accepted_clips_are_never_paid_again_unless_explicit_or_stale` |
| job_id se persiste de inmediato | `test_job_id_is_persisted_immediately_on_submit` (visible en `project.json` **mientras** el video aún se genera), `test_dubvoice_strict_flow_persists_job_id_first…` (`on_submit` antes del primer sondeo) |
| Errores tipados | `test_error_taxonomy_and_classification`, `test_dubvoice_strict_429_and_402_and_5xx_and_gateway_are_typed`, `test_google_quota_429_is_a_fatal_rejection_not_a_rate_limit`, `test_decide_*` |
| F5 termina `COMPLETED_WITH_WARNINGS` | `test_11_accepted_plus_1_needs_review…`, `test_autopilot_stops_before_f6_when_f5_has_warnings` |
| Sin fallback automático de baja calidad | `test_no_automatic_low_quality_fallback_ladder_or_still_image`, `test_google_quota_disables_google…`, `test_supervisor_does_not_salvage_with_a_still_image_automatically` (la imagen fija solo por acción explícita) |
| Restart G / H | `test_G_*` (job conocido ⇒ reconcilia, 0 POST), `test_G2_*` (Google por operation ID), `test_G3_*` (irreconciliable ⇒ `NEEDS_REVIEW`, 0 POST), `test_H_*` (`SUBMITTING` sin job_id ⇒ `NEEDS_REVIEW`, 0 POST) |
| Concurrencia y slot | `test_concurrency_never_exceeds_max_concurrent_video_jobs`, **`test_generation_slot_is_released_when_raw_is_saved_not_after_voice_unify_audit`** (con `MAX=1` y voz lenta, el 2º clip se envía mientras el 1º aún cambia la voz) |
| Canario del contrato | `test_canary_first_job_runs_alone…`, `test_contract_mismatch_stops_all_new_submissions` (1 solo POST; ni siquiera fallback) |
| Secretos | `test_secrets_never_reach_project_json_events_or_contract_logs`, y la aserción de clave-canario en el test del contrato |

## 8. Desviaciones respecto al plan (todas conservadoras)
1. **Orden post-proceso**: QC visual (Claude) **antes** de la voz/unify. Así no se paga cambio de voz de un clip que se va a rechazar. (El plan decía unify → auditoría → Claude.)
2. **`audio_state` tiene un valor extra `UNVERIFIED`** (Whisper caído ≠ audio malo).
3. **`source_start/source_end`** se derivan repartiendo la escena entre sus clips por el punto medio de las pausas (tramo *visual*), no solo con `t_start/t_end` (que es únicamente el habla). Si faltan datos de escena: `t_start/t_end`, y último recurso `target` heredado (`timeline_source` lo declara). El `target` heredado (inflado para español) se sigue usando **solo** para elegir la duración que se pide a Google.
4. **`2xx` sin `job_id`** ⇒ envío ambiguo (`NEEDS_REVIEW`, sin reenvío) y **congela** al proveedor si su contrato no está verificado. El plan decía "un reintento".
5. **Google no cuenta en `MAX_COST_PER_CLIP`** (se factura en USD, no en créditos DubVoice); se guarda `credits=0` para Google.
6. **Tras un `PROVIDER_TIMEOUT`**: si al clip no le cabe otra ventana completa (`MAX_TOTAL_GENERATION_TIME − usado < PROCESSING_TIMEOUT`, que con 12/20 min ocurre tras el primer timeout) se pasa **directo al fallback** en lugar de repetir el mismo proveedor. Sin fallback: una ventana corta.
7. **Estado global**: los problemas de audio no lo degradan a `COMPLETED_WITH_WARNINGS` (se listan en `audio_needs_fix`); solo clips `NEEDS_REVIEW`/`FAILED` lo hacen. F6 sigue exigiendo todos los clips `done`.
8. **Un `NEEDS_REVIEW` por límites/prompt/calidad/proveedor NO se reintenta en corridas automáticas** ("Generar clips pendientes", supervisor, autopilot); solo con el botón **Generar/Regenerar** del clip (acción explícita = nueva ronda pagada). Los `NEEDS_REVIEW` por fallo de post-proceso o job remoto conocido primero prueban la **recuperación gratis** (una vez); `?paid=1` fuerza generación nueva.
9. **`FAILED` de clip solo si nunca se pagó ni se envió nada**; si ya hubo intentos → `NEEDS_REVIEW`.
10. Añadidos no planeados: `tools/f5_report.py`, `tests/conftest.py`, lectura de `~/.zshrc`/`data/f5_config.json`, ajuste `video_fallback=false` respetado, `salvage_still` marcado obsoleto (2 líneas en `store.py`).
11. **5 tests preexistentes reescritos** porque fijaban la política vieja que usted retiró: `…retries_failures_audits_and_finishes` (ahora Claude solo reescribe el prompt y hace QC), `…autopilot_when_claude_is_down` (→ Claude caído conserva RAW y no regenera), `…salvages_with_voiceover…` (→ no hay imagen fija automática), `…google_quota…model_ladder` (→ sin escalera), `…retry_goes_to_google…` (→ 2 timeouts con job_id y luego Google).
12. `render_clip` (camino síncrono heredado, usado por `cli clip`) se conserva; F5 ya no lo usa.

## 9. Bugs encontrados durante la implementación (todos corregidos y cubiertos por test)
* Congelar/desactivar un proveedor se aplicaba **después** de liberar el slot: un segundo job podía colarse (ahora antes de liberar).
* El watchdog tenía un piso de 30 s que anulaba `PROCESSING_TIMEOUT` pequeño.
* La política contaba los intentos **antes** de registrar el `job_id` ⇒ el fallback se saltaba un intento.
* Un MP4 corrupto se habría aceptado en la reconciliación (ahora `raw=None`, `bad_raw`, `DOWNLOAD_ERROR`).
* Un `RATE_LIMIT` con job ya existente habría comprado otro job (ahora sigue sondeando el mismo).
* Un clip antiguo `done` + `stale` sin bloque `f5` no se regeneraba.
* La recuperación "gratis" se ofrecía aunque no hubiera nada que recuperar (bloqueaba el Regenerar explícito).
* Tensión de presupuesto **12 min de procesamiento vs 20 min por clip** (ver §10).
* Tests con orden de hilos no determinista (corregidos; suite estable 3/3).

## 10. Supuestos aún NO verificados (los resolverá la prueba real)
**Contrato DubVoice** (nada de esto está confirmado): nombre del campo del job (`task_id`/`id`/…), endpoint y parámetro de sondeo, valores reales de estado, campo de la URL final; si el sondeo cuenta dentro de los 10 req/min (`VIDEO_REQUESTS_PER_MINUTE=6` es un supuesto); si un job fallido se reembolsa (se asume `refund_assumed`); si soporta *idempotency keys*; si un `5xx` implica "no se creó job" (`502/504` se tratan como ambiguos); tiempos típicos (¿12 min es razonable?); máximo real de jobs simultáneos.
**Red real**: `WriteError/ConnectError` se consideran "no enviado" (seguro) y `ReadError/RemoteProtocolError/timeout` "enviado" (ambiguo): mapeo probado con sockets locales, no con DubVoice.
**Claude QC**: el prompt de QC visual solo se probó con respuestas simuladas.
**Tensión de tiempos**: con `PROCESSING_TIMEOUT=12 min` y `MAX_TOTAL_GENERATION_TIME=20 min`, tras un timeout solo caben ~8 min. Si el test muestra que DubVoice tarda ≤4 min, conviene bajar `PROCESSING_TIMEOUT` a 6–8 min para que quepan reintento + fallback.
**Línea de tiempo**: verificada leyendo `fragment.py` (tiempos absolutos del video original) y con datos simulados; **no** se abrió el proyecto real.

---

## 14. Instrucciones PRELIMINARES de la prueba real (3 clips) — NO ejecutadas

**Antes de empezar**
1. En la Mac: `git pull` de la rama, cierre y vuelva a abrir la app. Haga copia de la carpeta del proyecto (`data/projects/<id>/`).
2. Confirme créditos DubVoice y anótelos. Recomendado para la 1ª prueba, en `~/.zshrc` (o en `data/f5_config.json`):
   `export MAX_CONCURRENT_VIDEO_JOBS=1` y `export F5_FALLBACK_PROVIDER=none` (sin gastar en Google mientras se descubre el contrato). Reinicie la app.
3. El proyecto real (9 aceptados + 3 sin resolver) conserva sus clips como `ACCEPTED`. Use los botones **Generar/Regenerar** por clip (acción explícita) en este orden: **(a) clip simple**, **(b) clip con gesto/movimiento**, **(c) clip con interacción compleja**. **Uno a la vez.** No pulse dos veces el mismo botón: cada clic puede pagar.

**Durante cada clip** (otra terminal): `tail -f data/projects/<id>/f5_events.jsonl` y `data/projects/<id>/f5_contract.jsonl`.

**Puerta de decisión tras el clip (a)** — debe verse en `f5_contract.jsonl`: `submit` con `http_status` 200/201/202 y `json_shape` con el campo del job → evento `job_submitted` (job_id) → `poll` con la ruta que respondió → `status_transition` con los estados reales → `download` → `CONTRACT_VERIFIED`, y existir `data/contracts/dubvoice.json`.
* Si algo no coincide la app **congela** DubVoice ("DETENGO nuevos envíos") y no gasta más. **Deténgase** y envíeme `f5_contract.jsonl` (ya está sanitizado; no contiene claves).
* Si aparece `AMBIGUOUS_SUBMIT`: **no reintente**; revise el panel de DubVoice para ver si se cobró y avíseme.

**Registro por clip** (una fila por intento; lectura solamente):
`python tools/f5_report.py <id_proyecto> --clips 7.1,8.1,8.2` → hora de envío, **job_id**, proveedor/modelo, sondeos, tiempo hasta completar, duración recibida vs `target_duration` (y tramo origen), resultado del QC, reintentos, error tipado y créditos. Más: créditos en el panel de DubVoice antes/después, y `GET /api/projects/<id>/f5/summary`.

**Solo si los 3 clips funcionan**: suba a `MAX_CONCURRENT_VIDEO_JOBS=3` (o el máximo que el `x-ratelimit-*` capturado justifique), ajuste `PROCESSING_TIMEOUT` con los tiempos reales y escale al video completo.

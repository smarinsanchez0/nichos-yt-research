# PASO 3.2 — Contrato real de DubVoice y reconciliación de AMBIGUOUS_SUBMIT

## 1. Causa raíz del timeout (hechos vs hipótesis)
**Hechos confirmados**
* El canario #2 envió el POST, esperó 60.7 s y terminó en `ReadTimeout`. En `dubvoice._veo_strict` el timeout de lectura estaba fijado en código como `min(60, SUBMISSION_TIMEOUT)` ⇒ **60 s por diseño nuestro** (no era el valor configurado de 90 s).
* El panel de DubVoice muestra una generación nueva (Veo 3.1 Fast, 17:56, READY, 8 s): el servidor **recibió el POST, creó el job y lo terminó**; la respuesta se perdió del lado del cliente.
* `ReadTimeout` tras enviar ⇒ el POST pudo haber creado un job ⇒ `AMBIGUOUS_SUBMIT` (F5 no reintentó: correcto).

**Hipótesis (apoyadas por evidencia, NO confirmadas con una respuesta real)**
* `POST /api/v1/video` para Veo es **síncrono** (mantiene la petición abierta mientras se renderiza). Indicios: la documentación de DubVoice que usted pegó describe Veo como "Google, 60-120 s, 8 s fijo" y **no documenta ningún endpoint de consulta para video** (sí para TTS e imagen y `stock-video`); el POST de Grok está documentado explícitamente como "renders synchronously (~60-120 s) and returns the final `file_url`"; el código heredado usaba `post_timeout=600` y tenía una rama "respuesta sincrónica con URL" que produjo los 9 clips aceptados de la corrida real; y el job estaba READY en ~1 minuto.
* Qué devuelve exactamente ese POST (¿`file_url`/`video_url`? ¿algún `task_id`?) **sigue sin verificarse**: el ContractRecorder del canario solo registró el error.

## 2. Contrato DubVoice: lo documentado
| Tema | Evidencia |
|---|---|
| Submit | `POST /api/v1/video` (`prompt`, `model`, `aspect_ratio`, `resolution`, `ref_images`, `mode_image`; Veo `duration` ignorado, 8 s fijo) |
| Respuesta / ID | **No documentada** para video (estados genéricos `pending→processing→completed|failed`, campo `task_id` en TTS) |
| Sondeo de video | **No documentado** (solo `GET /api/v1/stock-video?job_id=` y TTS/imagen) |
| Listar / buscar generaciones | **No existe documentado** para video (`GET /api/v1/tts` lista solo TTS) |
| Idempotencia / id de cliente / metadata | **No documentado** (ningún `client_request_id`, `external_id` ni `Idempotency-Key`) |
| Webhook | `webhook_url` (HTTPS) al enviar: documentado, pero la app corre en `localhost` (sin URL pública) ⇒ no usable hoy |
| Límite | 10 req/min por clave para imagen/video/música; 429 con `Retry-After` / `X-RateLimit-*` |
| Saldo | `GET /api/v1/me` → `credits` (lectura gratuita documentada) |
| Reembolso | "Failures are refunded automatically" (documentado) |

No se hizo ninguna llamada real para descubrir nada (0 POST; tampoco se probó el frontend del panel).

## 3. Cómo funciona ahora AMBIGUOUS_SUBMIT → RECONCILING
```
SUBMITTING (intento persistido ANTES del POST)
   │ ReadTimeout / reset / 2xx sin id / 502-504
   ▼
AMBIGUOUS (attempt.status)  ── F5_AMBIGUOUS_SUBMIT_RETRIES=0: NUNCA otro POST
   ▼
RECONCILING (clip SUBMITTED + attempt.status=RECONCILING; solo LECTURAS, F5_RECONCILE_TIMEOUT=180 s, cada 10 s)
   ├─ proveedor sin endpoint de listado (DubVoice hoy) → NotSupported → NEEDS_REVIEW (AMBIGUOUS_SUBMIT, recuperable con /adopt)
   ├─ 0 candidatos al agotar el tiempo → NEEDS_REVIEW
   ├─ varios candidatos → NEEDS_REVIEW (no se elige ninguno)
   └─ 1 candidato inequívoco → se persiste job_id → sondea/descarga ESE job → RAW → VALIDATING → QC → ACCEPTED
```
* **Invariante de la única puerta** (`ClipDriver._attempt`): mientras exista un intento ambiguo sin resolver en la ronda, NO se crea otro intento/POST. Solo `F5_AMBIGUOUS_SUBMIT_RETRIES>0` (desactivado) o `paid=1` explícito (ronda nueva) lo permiten.
* Regenerar (botón, sin `paid`) sobre un clip con envío dudoso **no paga**; `paid=1` (API) es la única forma.
* Reinicio de la app: intento `SUBMITTING/RECONCILING` sin job_id → `NEEDS_REVIEW (AMBIGUOUS_SUBMIT)`; la siguiente corrida reintenta la reconciliación (≤3 veces, solo lecturas).

## 4. Política exacta de matching (`video_jobs.match_candidates`)
Un candidato remoto solo cuenta si **todo** se cumple (nunca solo por la hora):
1. mismo **modelo**;
2. tiene **marca de tiempo** y cae en la ventana `[creación_del_intento − 30 s, creación + SUBMISSION_TIMEOUT + 60 s]`;
3. su id/URL **no pertenece a otro intento** del proyecto;
4. si expone el **prompt**, coincide (igual o prefijo ≥ 60 caracteres);
5. exactamente **1** candidato; si hay más → `MULTIPLE`; si no expone el prompt y otro envío nuestro al mismo modelo se solapa en la ventana → `MULTIPLE`.
Tras descargar, el video aún debe pasar la **huella** (`_verify_fingerprint`): duración del modelo (Veo 8 s ± 1) y primer fotograma ≥ 0.80 de similitud con el start frame de **esta** escena y **estrictamente más parecido** a él que al de cualquier otra escena (los empates fallan). Si falla: `ADOPTION_REJECTED`, el archivo queda aparte (`bad_raw`) y nada se asocia.

## 5. Recuperación manual sin pagar (`POST /api/projects/<id>/videos/<i>/<j>/adopt`)
Cuerpo: `{"result_url": "https://…mp4", "job_id": "…"}` (al menos uno; `http://` solo en loopback para pruebas). Condiciones: clip en `NEEDS_REVIEW` por envío/job remoto dudoso y sin RAW asociado; el handle no puede pertenecer a otro intento; después se aplica la huella anterior. **No hay POST**: se descarga esa URL y sigue el flujo normal (QC, voz, auditoría).

## 6. Timeouts (separados)
| Timeout | Valor por defecto | Nota |
|---|---|---|
| connect (POST) | 10 s (`F5_SUBMISSION_CONNECT_TIMEOUT`) | |
| read/total del POST | **300 s** (`SUBMISSION_TIMEOUT`, antes 60 s efectivos) | Veo documentado 60-120 s; 300 s ≈ 2.5× el máximo documentado. **Un timeout de POST sigue siendo AMBIGUO.** |
| poll (por petición) | 20 s | |
| processing | 720 s | |
| download | 180 s | |
| reconciliación | 180 s (`F5_RECONCILE_TIMEOUT`) | solo lecturas |
| por clip | 1200 s (`MAX_TOTAL_GENERATION_TIME`) | |
| corrida completa | 7200 s (`MAX_PROJECT_GENERATION_TIME`) | al vencer cancela; los jobs se conservan y se reconcilian |

## 7. POST sincrónico (si se confirma)
Si el POST devuelve directamente la URL, F5 persiste un id sintético `sync-…` + `result_url` **antes** de descargar; un fallo de descarga reintenta solo la descarga. El contrato **no** se marca `verified` (falta identificación/sondeo): queda en `observed.mode="sync_post"` y la puerta canario sigue en 1 job a la vez.

## 8. Qué falta para marcar `dubvoice_verified=true`
Una respuesta real del POST (forma del JSON y campo de id/URL), el endpoint y parámetro de sondeo (si existe), los valores de estado reales, el campo de URL final, y —para reconciliar automáticamente— un endpoint de listado/búsqueda o una clave de idempotencia. Hoy: `verified=false`.

## 9. PASO 3.2.1 — adopción de un MP4 local ya descargado
Arquitectura **A** (local, sin HTTP): `video_jobs.adopt_local()` + `tools/f5_adopt_local.py`. **No existe endpoint que reciba rutas** (el único `/adopt` HTTP sigue siendo el de URL remota `https://`; rechaza `file://`, rutas y `http://` no loopback).
* Validación previa, sin tocar el estado: archivo existente, regular, `.mp4`, 1 KB–300 MB, MP4 de video válido (ffprobe).
* Se COPIA a `data/projects/<id>/adopt_inbox/` y se calcula su sha256 (handle único en el proyecto).
* Reutiliza el núcleo de `adopt_remote` (`_adopt_common`) y el mismo pipeline: `_raw_saved` (RAW atómico) → huella visual (duración del modelo + start frame de esta escena vs. las demás, empates fallan) → QC visual → voz → auditoría. Nada se acepta "por dar una ruta".
* **Resuelve el intento ambiguo existente** (p. ej. `a2`): no crea intento ni pago (`paid_attempts` y créditos no cambian); conserva `submit_ambiguous`, `possible_duplicate`, `error_type` y añade `resolved_ambiguity {from_status: AMBIGUOUS, by: local_file}`.
* El UUID del nombre del archivo es solo `original_filename`; el `job_id` es `local-<sha256[:16]>` (`job_id_kind=local_file`).
* `--no-voice` omite el cambio de voz (se repite luego con `retry_voice`; `audio_state=NEEDS_FIX`).

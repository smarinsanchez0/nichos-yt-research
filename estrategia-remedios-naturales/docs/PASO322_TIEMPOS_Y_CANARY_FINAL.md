# PASO 3.2.2 — Telemetría de tiempos correcta + canario final de contrato DubVoice

## 1. Causa del `t_gen = 7094 s` falso
`tools/f5_report.py` (y `summarize`) calculaban `completed_at − submitted_at`. En un job **adoptado** `submitted_at` se fija al instante del POST original (17:56) y `completed_at` al de la adopción (19:54): la diferencia es el *retraso de recuperación*, no lo que tardó DubVoice (que se desconoce porque se perdió la respuesta).

## 2. Semántica nueva (`video_jobs.attempt_timing`, calculada al leer: no se modifica el historial)
| Campo | Significado |
|---|---|
| `submit_started_at` | inicio del POST (= `created_at`; el intento se persiste antes de enviar) |
| `submit_response_at` | llegada de la respuesta del POST (si existe) |
| `remote_completed_at` | solo si se conoce (POST síncrono: la respuesta ya trae el resultado) |
| `download_completed_at` | RAW descargado y guardado |
| `adopted_at` / `reconciled_at` | recuperación manual / automática |
| `submit_ack_seconds` | POST → respuesta (solo ciclo observado) |
| `generation_seconds` | envío → resultado/descarga, **solo** si el ciclo se observó completo en este proceso. Si el job se recuperó (adopción, reconciliación, reanudación): `None` |
| `recovery_delay_seconds` | envío original → recuperación; **no** es tiempo de generación |

Reporte (`tools/f5_report.py`): columnas `ack`, `t_gen` y `recup`; `t_gen = -` y `recup = 7094s` para el clip adoptado.
`summarize`: `avg/max_generation_seconds` solo con tiempos observados; `recovery_delays_seconds` y `max_recovery_delay_seconds` aparte; cada intento lleva su bloque `timing`.

## 3. ContractRecorder (`f5_contract.jsonl`, sanitizado)
**Guarda por petición:** `ts`, `op`, `method`, `url` (sin query), `request_shape` (claves y valores cortos, nunca base64), `elapsed_ms`/`elapsed_seconds`, `http_status`, `content_type`, `content_length`, `header_names` (solo NOMBRES de todas las cabeceras de respuesta), `headers` (lista blanca: content-type, retry-after, x-request-id, date, x-ratelimit*), `json_shape` (árbol de tipos), `body` (JSON sanitizado) y `extracted`: `json_keys` (ruta→tipo), `ids` (task/job/id…), `status` (ruta+valor), `model`, `durations` y `urls` (host, ruta sin query, `had_query`). También `status_transition`, `UNRECOGNIZED_STATUS`, `download`, `CONTRACT_OBSERVED`/`CONTRACT_VERIFIED`.
**Redacta/omite:** cabeceras de PETICIÓN (Authorization, X-API-Key…) — nunca se registran; valores de cabeceras de respuesta fuera de la lista blanca (cookies, tokens…); claves JSON con nombre `key|token|secret|authorization|signature|password|cookie|credential…` → `<redacted>`; cualquier clave/token conocido (entorno, `~/.zshrc`) en cualquier texto; query de las URLs (donde viajan firmas/tokens) → `?<query-redacted>`; base64/data-URIs.
La URL completa de resultado solo existe en el estado privado del intento (`result_url` en `project.json`) porque hace falta para descargar; nunca se imprime ni aparece en logs/eventos/reportes/endpoints (`/f5/clip` la elimina; se muestra `result_url_redacted`).

## 4. Timeouts del canario (sin cambios de política)
connect 10 s · lectura/total del POST 300 s · poll 20 s · procesamiento 720 s · por clip 1200 s. Un `ReadTimeout` tras enviar = `AMBIGUOUS_SUBMIT`, 0 reintentos, `NEEDS_REVIEW`.

## 5. Modo canario (`tools/f5_canary.py`)
Un clip, concurrencia 1, sin fallback, `F5_AMBIGUOUS_SUBMIT_RETRIES=0`, `F5_SINGLE_POST=1` (un clip no puede tener más de UN intento por ronda: ni tras un 429 ni tras un error de conexión), `F5_SKIP_VOICE=1`, sin F6, sin saldo (salvo `--balance`), modelo `veo-3.1-fast` exigido, `dubvoice.veo` envuelto (la 2ª llamada falla) y Google bloqueado. Por defecto trabaja sobre una **copia** del proyecto. Sin `--yes-i-accept-one-paid-post` es un simulacro sin red.
Imprime en vivo: `SUBMITTING` → `HTTP <status> recibido en <s> s` → `contrato capturado` → job → `RAW descargado y guardado · provider_duration · target_duration` → `QC` → estado final.

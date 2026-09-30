# ESTRATEGIA REMEDIOS NATURALES — Reporte de auditoría y documentación

Fecha del reporte: 30-sep-2026 · Rama: `claude/ai-video-replication-app-wtfzso` · Código: ~3.800 líneas Python + interfaz web · 21 pruebas automáticas

> **Cómo leer este reporte.** Cada afirmación está marcada como:
> ✅ **Verificado con tus ejecuciones reales** (tu Mac) · 🧪 **Verificado solo con servicios simulados** (mi entorno de pruebas, sin gastar API) · ❓ **No verificado / supuesto**.
> Desde mi entorno en la nube nunca pude llamar a DubVoice, Google ni Anthropic con tus keys, así que todo lo que dice 🧪 puede comportarse distinto con las APIs reales.

---

## 1. Resumen ejecutivo

**Objetivo:** recibir un video en inglés y la foto de un avatar, y entregar un Reel 9:16 en español con ese avatar, una sola voz de IA, subtítulos y edición, con el menor gasto y tiempo posibles.

**Estado real hoy**

| Fase | Estado | Evidencia |
|---|---|---|
| 1 Avatar | ✅ Funciona | Perfil del avatar generado en tu Mac |
| 2 Análisis (transcripción, traducción, escenas, prompts) | ✅ Funciona | Corrió completa: 10 escenas de tu video de 46 s |
| 3 Imágenes del avatar | ⚠️ Funciona, calidad sujeta a tu revisión | Imágenes generadas; se corrigió 4 veces el método (ver §4) |
| 4 Guion y prompts de video | ✅ Funciona | 12 clips generados en el plan |
| 5 Clips de video | ⚠️ **Parcial: 9 de 12 clips aceptados** | 3 clips (escena 7 clip 1; escena 8 clips 1 y 2) siguen sin resolverse |
| 6 Edición final | ❓ No ejecutada aún con tu video real | Solo probada con simulación |

**El cuello de botella es la Fase 5**, y no es una sola causa: es una combinación de (a) proveedores de video lentos o que se atascan, (b) documentación incompleta de DubVoice, (c) problemas de instalación en tu Mac (chip Apple) que consumieron tiempo, y (d) mi entorno de pruebas que no puede llamar a las APIs reales, por lo que varios arreglos se probaron "a ciegas" y se afinaron con tus errores reales.

---

## 2. Qué se construyó (arquitectura)

```
video EN + foto avatar
   │
F1  Claude (visión) describe el avatar y su perfil de voz
F2  Transcripción (Whisper local o ElevenLabs) → traducción (Claude) → detección de escenas (ffmpeg)
    → Claude lee cada captura y escribe el prompt de imagen (META-PROMPT 1 y 2 de tu guía)
F3  Nano Banana Pro (DubVoice; respaldo Google/Kie) → imagen por escena; Claude revisa cada imagen
F4  Reparte el guion por escena/clip con marcas de tiempo; traduce al español; prompt de video (META-PROMPT 3)
F5  DubVoice Veo 3.1 (respaldo: Google Veo) genera cada clip · voz unificada · SUPERVISOR CLAUDE audita y decide
F6  ffmpeg: recorta silencios, une, subtítulos Poppins (blanco/amarillo, trazo negro), audio normalizado
```

**Formas de usarlo**
1. **App visual** (`http://127.0.0.1:8000` o el ícono de Mac): las 6 fases con botones.
2. **Modo automático** (`python -m app.cli run --video V --avatar A`): una orden, sin pantalla.
3. **Skill `/remedios`** (Claude Code): invoca el modo automático, vigila y audita.

**Archivos principales** (`estrategia-remedios-naturales/`)

| Ruta | Función |
|---|---|
| `app/main.py` | API web (FastAPI) y rutas de cada fase |
| `app/phases/analysis.py` | Fase 2 (transcripción, traducción, escenas, meta-prompts de imagen) |
| `app/phases/metaprompts.py` | Los 3 meta-prompts de tu guía + reglas fijas de Veo |
| `app/phases/images.py` | Fase 3: generación, modos (guía/swap/blur/solo texto), revisión con Claude, respaldo |
| `app/phases/fragment.py` | Fase 4: fragmentación del guion y prompts de video |
| `app/phases/videos.py` | Fase 5: generar clip, cambio de voz, estimador de créditos |
| `app/phases/supervisor.py` | **Supervisor Claude** de la Fase 5 |
| `app/phases/editing.py` | Fase 6: edición y subtítulos |
| `app/services/` | Conectores: `claude`, `dubvoice`, `google_veo`, `gemini`, `kie`, `eleven`, `stt` |
| `app/autopilot.py`, `app/cli.py` | Modo automático y comandos (`doctor`, `run`, `status`, `edit`, `redo-clip`, `redo-image`) |
| `app/demo.py` | Servicios simulados (`--demo`) para probar sin gastar |
| `mac/` | Instalador del ícono, lanzador con auto-reparación, instalador de la skill |
| `.claude/skills/remedios/SKILL.md` (raíz del repo) | La skill `/remedios` |
| `tests/` | 21 pruebas automáticas |

---

## 3. Auditoría por fase

### Fase 1 — Avatar ✅
- Claude (visión) devuelve descripción (en inglés, para prompts), género y perfil de voz.
- **Problema hallado:** la key de Anthropic no estaba ligada a un *workspace* → error 400. **Solución:** soporte de `ANTHROPIC_WORKSPACE_ID`.

### Fase 2 — Análisis ✅
- Transcripción con marcas por palabra; traducción por frases; escenas por detección de cortes de ffmpeg (máx. 8 s por escena); lectura de frames por Claude; meta-prompts por captura.
- **Problemas hallados y resueltos:**
  - ElevenLabs bloqueó la key por un **pago pendiente** → transcripción **local gratis** (Whisper) como opción por defecto.
  - `faster-whisper` no cargaba en tu Mac: pieza `av` compilada para Intel en un Mac Apple; pip viejo (21.2.4) → se actualiza `pip` y se reconstruye el entorno.
  - Avisos "divide by zero… matmul" (numpy 2 en Apple Silicon) → `numpy<2` fijado. ❓ **Pendiente de confirmar** que la transcripción de tu video salió correcta (te pedí leerla).

### Fase 3 — Imágenes ⚠️ (la que más se rehízo)
Problema central: **la IA copiaba a la persona del video original** (cara, ropa) en lugar de usar tu avatar, o usaba la pose de tu foto (manos cruzadas) en lugar de la acción del frame.

| Intento | Método | Resultado |
|---|---|---|
| 1 | Avatar + frame original completo, "recrear lo más exacto posible" | Copiaba al hombre del video |
| 2 | Prompts sin ropa/rasgos del original; acción como instrucción principal | Avatar aparecía, pose repetida |
| 3 | Frame **difuminado** como guía | Evita copiar a la persona; pose aproximada |
| 4 | Modo "swap": editar el frame reemplazando a la persona | Mantiene pose exacta |
| 5 (actual) | **Método de tu guía**: Claude mira la captura y escribe el meta-prompt; imagen 1 = captura + avatar; siguientes = Imagen A (captura) + Imagen B (imagen anterior) + avatar; revisión con Claude y hasta 2 correcciones automáticas | Es el método por defecto; 🧪 probado con simulación, tu revisión visual pendiente |
- **Robustez añadida:** reintentos, respaldo automático a otro proveedor, estado por escena, botón "destrabar", modo rápido (imágenes en paralelo tras la primera).
- **Hallazgo importante:** varias imágenes se generaron con "respaldo: Kie" porque DubVoice falló; el motivo exacto no se me mostró. ❓ Causa del fallo de DubVoice en imágenes **sin confirmar**.

### Fase 4 — Guion y prompts de video ✅
- Reparte palabras por escena con las marcas de tiempo; clips de máximo ~5,6 s de habla en español (el español dura ~25% más); traduce cada clip; prompt final con las reglas fijas de tu guía (iPhone, cámara estática, hiperrealista, sin música, cierre "El start frame proporcionado define…").

### Fase 5 — Clips de video ⚠️ → ver §4 completo

### Fase 6 — Edición ❓
- Recorta silencios, une, subtítulos Poppins (blanco, trazo negro, palabras clave en amarillo), audio a -16 LUFS, verifica coincidencia con el guion.
- 🧪 Probada con simulación (1080×1920). ❓ **Aún no se ha ejecutado con tus clips reales.**
- **Problema hallado y resuelto:** ffmpeg buscaba un binario de Intel que no existe en tu Mac → ahora se prueba cada ffmpeg disponible y se usa el que realmente funciona (te salió ffmpeg 6.0).

---

## 4. Fase 5 en detalle: intentos, tardanzas y por qué

### 4.1 Cronología de intentos

| # | Qué se intentó | Qué pasó | Por qué |
|---|---|---|---|
| 1 | Veo 3 vía **Kie.ai**, espera hasta 20 min; voz unificada con ElevenLabs | Clip "en cola" 20 min sin resultado | Kie devolvía estado "0 (procesando)" sin terminar; mi espera era muy larga y no informaba |
| 2 | Cambio de voz con ElevenLabs | Error de pago (cuenta bloqueada) | Factura pendiente en tu cuenta de ElevenLabs |
| 3 | **Duración variable** (pediste 3/5 s en lugar de 8 s fijos) | Veo solo genera 8 s (DubVoice y Kie) | Limitación del modelo; se agregó **Omni Flash** (4/6/8/10 s) — ❓ voz/lipsync no verificados |
| 4 | Cambio de voz con **DubVoice** | `401 No autorizado` en ese endpoint | ❓ Sin confirmar (¿permiso de la key o forma de llamar?); se implementaron 4 formas de llamada y se recuerda la que funcione |
| 5 | Kie sigue sin entregar tras 10 min con progreso visible | Sigue "estado 0" | Problema del servicio Kie → **se eliminó Kie del video**; DubVoice pasa a proveedor oficial |
| 6 | DubVoice: límite de 3 en paralelo y 10 solicitudes/min | Errores 429 | Se agregó espaciado entre solicitudes y espera automática |
| 7 | `Connection reset by peer` al enviar el clip | Fallo sin reintento | Corte de conexión; se agregó reintento con espera y se redujo el tamaño de la imagen |
| 8 | **Supervisor Claude**: 1 clic, audita cada clip (duración, audio, texto hablado vs diálogo, 3 fotogramas), decide aceptar/reintentar/cancelar; límites de intentos, créditos y tiempo | 9 de 12 clips aceptados | Funciona para los clips que el proveedor entrega |
| 9 | Quedan 3 clips (escena 7 y 8) sin terminar tras 3 intentos | "Clips sin resolver" | Sospecha: prompts del cierre de venta rechazados por filtros, o cola de DubVoice — ❓ **causa exacta sin confirmar**, faltan las líneas ❌/🚫 de la bitácora |
| 10 | **Respaldo Google Veo 3.1** (tu key de Google AI Studio): los reintentos tras un fallo/atasco van directo a Google; límite de atasco de 7 min | Implementado, 🧪 probado | ❓ No probado contra Google real |

### 4.2 Por qué tarda tanto (causas, ordenadas por peso)
1. **Latencia del proveedor de video.** Veo tarda normalmente 1–4 min por clip; con 12 clips y 3 en paralelo: ~16 min en el mejor caso. Si un clip se atasca, el proveedor no da señal de error, solo "procesando".
2. **Documentación incompleta de DubVoice.** No dice cómo consultar el estado de un video ni qué devuelve; el conector prueba rutas conocidas. Cada corrección dependió de un error tuyo.
3. **Problemas de instalación en tu Mac (chip Apple) que consumieron horas:** pip viejo, piezas `av` y `pydantic_core` para Intel, ffmpeg de Intel, numpy 2, servidor viejo sin reiniciar. Ya corregidos y con auto-reparación en el lanzador.
4. **Mi entorno no llama a las APIs reales.** No puedo ver lo que ve tu Mac; pruebo con simuladores y ajusto con tus errores. Esto alarga cada ciclo.
5. **Cuentas y créditos:** ElevenLabs bloqueado por pago; una key de Anthropic sin workspace.
6. **Cambios de método en Fase 3** (5 versiones) para que el avatar respete tu identidad y la acción del frame.

### 4.3 Mediciones que faltan (❓)
No tenemos todavía: tiempo real por clip en DubVoice, tasa de fallos por escena, costo real gastado, ni la causa de los 3 clips atascados. La bitácora del supervisor (panel de la Fase 5) las registrará.

---

## 5. Registro de problemas y soluciones (resumen)

| Síntoma | Causa | Solución | Estado |
|---|---|---|---|
| "not scoped to a workspace" | Key de Anthropic sin workspace | `ANTHROPIC_WORKSPACE_ID` | ✅ |
| ElevenLabs 401 pago | Factura pendiente | Whisper local + DubVoice para la voz | ✅ evitado |
| DubVoice 429 | Límite 3 paralelo / 10 por min | Espaciado + reintento | ✅ 🧪 |
| Imagen copia al original | Frame completo como referencia | Método de tu guía + revisión Claude | ⚠️ pendiente de tu revisión visual |
| Clip "en cola" 20 min (Kie) | Kie no entrega | Kie eliminado del video | ✅ |
| 401 en cambio de voz | ❓ | 4 formas de llamada; si falla, se conserva la voz de Veo | ❓ |
| `Connection reset by peer` | Corte de conexión | Reintento con espera | 🧪 |
| `faster-whisper` no carga | `av` de Intel | Reconstruir entorno con pip nuevo | ✅ |
| `pydantic_core` no importa | Mismo problema | Lanzador fuerza arm64 y se auto-repara | 🧪 (pendiente de probar el ícono) |
| ffmpeg x86_64 inexistente | Ruta de Intel | Buscar ffmpeg que funcione | ✅ |
| Transcripción con avisos de numpy | numpy 2 en Apple Silicon | `numpy<2` | ❓ verificar texto |
| 3 clips sin resolver | ❓ | Respaldo Google + motivo por clip | ❓ |

---

## 6. Costos (orientativos — confírmalos en tus paneles)

- **DubVoice:** créditos publicados: clip Veo fast 7.500; imagen Nano Banana Pro 3.500; Omni Flash 4.688–9.375 según 4/6/8/10 s; cambio de voz 2.000/min. Con el paquete de 10M ($34,99 ≈ $0,0035 por 1.000 créditos) un clip cuesta ~$0,03.
- **Google Veo (respaldo):** ~$0,4–0,8 por clip (dato de terceros; verifica).
- **Un Reel de ~46 s (10 imágenes + 12 clips):** ~110.000–125.000 créditos de DubVoice (~$0,4 a $2,5 según paquete) + Claude API (centavos) + respaldos de Google si se usan.
- **Google Flow (plan web):** no sirve para la app (no tiene API).

Enlaces de saldo: https://www.dubvoice.ai/dashboard/balance · https://aistudio.google.com/usage · https://console.anthropic.com/settings/billing

---

## 7. Seguridad

- Las keys se leen de `~/.zshrc`; nunca se muestran completas.
- **Se compartió una key `sk_live_…` de DubVoice en el chat:** conviene **revocarla y crear otra** en `dubvoice.ai/dashboard/api-docs`.
- Variables de entorno de un entorno en la nube pueden ser visibles para quienes lo usen: no compartir el entorno si contiene keys.
- Límites de gasto del supervisor: 3 intentos por clip, presupuesto ≈ 2,2× el estimado, 7 min por clip, 75 min por corrida.

---

## 8. Pendientes y recomendaciones

1. **Resolver los 3 clips** (escenas 7 y 8): `git pull`, reiniciar, pulsar de nuevo el supervisor; pasarme las líneas ❌/🚫 si vuelven a fallar.
2. **Reescribir el prompt del cierre de venta** si es rechazado (evitar "cura", "sana", promesas de salud).
3. **Ejecutar Fase 6** con los clips reales y revisar subtítulos y voz.
4. **Verificar la transcripción** del video (por el aviso de numpy).
5. **Confirmar qué falló en DubVoice** al generar imágenes (abrir "Por qué falló" en una tarjeta).
6. **Probar el ícono de Mac** con el lanzador nuevo; si falla, pasarme el aviso.
7. **Fusionar a `main`** el último commit (respaldo Google Veo): `main` está en `ea9a169`; la rama tiene 1 commit adicional.
8. Para trabajar dentro del chat de la nube: configurar en el entorno los dominios (`dubvoice.ai`, `www.dubvoice.ai`, `*.supabase.co`, `api.anthropic.com`, `generativelanguage.googleapis.com`, `huggingface.co`, `*.huggingface.co`, `*.hf.co`) y las variables `ANTHROPIC_API_KEY`, `DUBVOICE_API_KEY`, `GOOGLE_API_KEY`, y abrir una sesión nueva.

---

## 9. Guía rápida de operación

**Instalar/actualizar (Mac):** `cd ~/nichos-yt-research && git pull` · `bash estrategia-remedios-naturales/mac/instalar_mac.sh` (ícono) · `bash estrategia-remedios-naturales/mac/instalar_skill.sh` (skill).
**Comprobar entorno:** `.venv/bin/python -m app.cli doctor`.
**Prueba sin gastar:** `.venv/bin/python -m app.cli run --demo`.
**Ejecución real:** `.venv/bin/python -m app.cli run --video "RUTA/video.mp4" --avatar "RUTA/avatar.jpg" --lang es` (o `/remedios` en Claude Code).
**Reanudar:** `--project <ID>`. **Rehacer una escena:** `redo-image` / `redo-clip` y luego `edit`.
**Ajustes útiles (app):** modelo de imagen, referencia de escena, revisión automática, modelo de video, respaldo automático (`video_fallback`), voz.

---
name: remedios
description: REMEDIOS — recibe un video en inglés y la foto de un avatar y entrega el Reel final editado (9:16) en español con el avatar, voz de IA consistente, subtítulos y silencios recortados, auditando cada escena. Úsala cuando el usuario escriba /remedios, "skill remedios" o pida replicar un video en inglés con su avatar para el nicho de remedios naturales.
argument-hint: "<video_en_ingles> <imagen_avatar> [--lang es|en] [--name X]   (o: demo)"
allowed-tools: Bash, Read
---

# /remedios — video en inglés + foto de avatar → Reel final

Proyecto (código y API keys ya configuradas en `~/.zshrc`): `./estrategia-remedios-naturales`
Argumentos recibidos: `$ARGUMENTS`

## Qué hace
Ejecuta las 6 fases de ESTRATEGIA REMEDIOS NATURALES sin pantalla: analiza el avatar → transcribe/traduce y separa escenas del video →
genera las imágenes del avatar con el método de la guía (Nano Banana Pro, auditadas por Claude) → reparte el guion por clip →
genera los clips con DubVoice/Veo 3.1 bajo el **supervisor Claude** (audita, reintenta, cancela atascos) con **una sola voz de IA** para todo el video →
edita (une, recorta silencios, subtítulos Poppins blancos con amarillo para palabras clave) y entrega el MP4.

## Pasos que debes seguir

### 1. Entradas
- Necesitas **dos rutas**: el video en inglés y la imagen del avatar (acepta rutas con espacios/comillas o arrastradas). Si falta alguna, pídela en una línea.
- Si el argumento es `demo` (o dice "prueba sin gastar"), salta a **Modo demo**.
- Opciones útiles: `--lang es` (por defecto; `en` deja el video en inglés), `--name`, `--voice-id`, `--voice-gender male|female`, `--notes "texto para todas las escenas"`, `--sequential` (imágenes encadenadas: más lento, más continuidad).

### 2. Preparar entorno (una vez)
```bash
cd "./estrategia-remedios-naturales" 2>/dev/null || cd ./estrategia-remedios-naturales
[ -x .venv/bin/python ] || (python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt)
.venv/bin/python -m app.cli doctor
```
Si `doctor` marca ❌: explica en una frase qué falta (key de Anthropic/DubVoice en `~/.zshrc`, ffmpeg, dependencias) y corrige lo que puedas
(p. ej. `pip install -r requirements.txt`). Nunca imprimas ni pidas keys en el chat. Si Anthropic responde "not scoped to a workspace",
el usuario debe agregar `export ANTHROPIC_WORKSPACE_ID=wrkspc_...` a `~/.zshrc`.

### 3. Aviso de gasto (una línea) y lanzamiento
Estima: clips ≈ duración_del_video / 6 s; cada clip ≈ 7.500 créditos DubVoice + cada imagen ≈ 3.500. Dilo en una línea.
Si el video dura más de 90 s, pide confirmación antes de gastar; si no, empieza. Lánzalo **en segundo plano** y guarda el log:
```bash
.venv/bin/python -m app.cli run --video "<VIDEO>" --avatar "<AVATAR>" --lang es 2>&1 | tee /tmp/remedios_run.log
```
(usa `run_in_background: true` en Bash). Mientras corre, lee el log cada 1–2 minutos y resume en español los cambios de fase
(F1 avatar → F2 análisis → F3 imágenes → F4 guion → F5 clips/supervisor → F6 edición). No repitas líneas ni inundes al usuario.
Si el proceso falla, **el proyecto queda guardado**: reanuda con `--project <ID>` (el ID aparece en la primera línea del log) en vez de empezar de cero.

### 4. Auditoría final (tú también auditas)
Cuando el log termine con `RESULT_JSON: {"ok": true, ...}`:
1. Lee `contact_sheet` (una imagen con fotogramas del video final) con la herramienta Read y comprueba: ¿es siempre el mismo avatar (cara, ropa)?
   ¿coincide el ambiente entre escenas? ¿hay subtítulos legibles (blanco, trazo negro, palabras clave amarillas) sin texto original quemado?
2. Lee `final/report.json` del proyecto (ruta en `data/projects/<ID>/final/report.json`): revisa `warnings`, `script_match` (>0.8 es bueno),
   qué clips necesitaron varios intentos y qué imágenes la revisión no confirmó.
3. Si hay una escena claramente mala, arréglala **dirigido** (máximo 2 rondas salvo que el usuario pida más):
   - imagen: `.venv/bin/python -m app.cli redo-image --project <ID> --scene N --notes "cambio concreto"`
   - clip: `.venv/bin/python -m app.cli redo-clip --project <ID> --scene N --clip K`
   - luego rehacer el montaje: `.venv/bin/python -m app.cli edit --project <ID>`

### 5. Entrega
Responde en español, breve: **ruta del MP4 final** (`video` del RESULT_JSON, carpeta `~/Movies/REMEDIOS` en Mac), duración, voz usada,
minutos que tardó, y una lista corta de avisos reales (no inventes). Si estás en un entorno que permite enviar archivos, envía el MP4.
Ofrece rehacer una escena concreta si algo no convence. No digas que "todo está perfecto" si el reporte trae avisos.

## Modo demo (sin gastar créditos)
```bash
.venv/bin/python -m app.cli run --demo --name prueba
```
Usa servicios simulados y un video/avatar de prueba: sirve para comprobar que el flujo, la edición y la entrega funcionan.
Aclara siempre que el demo NO usa las IAs reales.

## Notas
- La app visual sigue funcionando (`./run.sh` o el ícono de Mac); comparten proyectos y código.
- Idioma por defecto del video final: español (el original en inglés se traduce clip por clip). Con `--lang en` queda en inglés.
- Límites de seguridad ya integrados: 3 intentos por clip, presupuesto de créditos, 12 min por clip, 75 min por corrida.

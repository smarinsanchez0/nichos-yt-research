# ESTRATEGIA REMEDIOS NATURALES

Replica un video (Reel 9:16, en inglés) con tu avatar de IA, escena por escena, en 6 fases.

```bash
./run.sh          # abre http://127.0.0.1:8000
```
Requisitos: Python 3.10+. ffmpeg (`brew install ffmpeg`; si no está, se usa el de `imageio-ffmpeg`).

## App para Mac (con logo)
```bash
bash mac/instalar_mac.sh     # crea "ESTRATEGIA REMEDIOS NATURALES.app" en /Applications
```
Doble clic para abrir (arrástrala al Dock). La primera vez instala dependencias (2-3 min). Cerrar la app (Cmd+Q en el Dock) apaga el servidor. Logs: `~/Library/Logs/EstrategiaRemediosNaturales.log`. La app se actualiza sola con `git pull` (reinstala dependencias si cambian). Si mueves la carpeta del proyecto, vuelve a ejecutar el instalador.

## API keys
Se importan solas de `~/.zshrc` (también `.zprofile`, `.zshenv`, `.bashrc`, `.env`). Nombres aceptados:
`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `KIE_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, `PEXELS_API_KEY`, `DUBVOICE_API_KEY`.
La barra superior muestra cuáles detectó (nunca se muestran completas).

## Fases
| Fase | Qué hace | Servicio |
|---|---|---|
| 1 Avatar | Subes la foto; Claude la describe y propone perfil de voz | Claude (visión) |
| 2 Análisis | Transcribe (palabra por palabra) +1, traduce +1, detecta escenas y lee los frames +1, redacta prompts de imagen +1 | Whisper local (gratis) o ElevenLabs Scribe, ffmpeg, Claude |
| 3 Imágenes | Nano Banana pone al avatar en la pose/decorado de cada frame original; retocar con prompt, regenerar, aprobar | Google AI Studio (`gemini-2.5-flash-image`) o Kie.ai |
| 4 Guion | Reparte el guion por imagen con tiempos exactos y crea el prompt de video (diálogo + acción) | Claude |
| 5 Videos | Anima cada imagen (Veo 3 fast, 9:16, con voz) y unifica la voz en todos los clips | DubVoice (video, único proveedor); cambio de voz con DubVoice o ElevenLabs |
| 6 Edición | Recorta silencios, une en orden, subtítulos Poppins (blanco, trazo negro, palabras clave amarillas), audio a -16 LUFS | ffmpeg, Scribe, Claude |

Salida: `data/projects/<id>/final/reel_final.mp4` (1080×1920).

## Supervisor Claude (Fase 5, 1 clic)
`app/phases/supervisor.py`: lanza todos los clips (máx. 3 en paralelo, respetando el límite de DubVoice), audita cada resultado
(duración, audio, texto hablado vs diálogo, 3 fotogramas comparados con la imagen aprobada) y Claude decide: aceptar, reintentar con otro
prompt/modelo/duración, cancelar atascos o descartar. Límites duros: 3 intentos por clip, presupuesto de créditos, 12 min por clip, 75 min en total.
Si Claude no responde pasa a piloto automático. La bitácora se ve en vivo en la app.

## Método de la guía (guia_creacion_video_IA)
Los 3 meta-prompts están en `app/phases/metaprompts.py`: Claude mira la CAPTURA de cada clip y escribe el prompt.
1. Clip 1 (start frame): Nano Banana Pro recibe captura + avatar. Se revisa/retoca/aprueba antes de seguir.
2. Clips siguientes: Imagen A (captura = acción) + Imagen B (imagen anterior generada = personaje y ambiente) (+ Imagen C = foto del avatar).
3. Veo 3: start frame + prompt con reglas fijas (iPhone, cámara estática, hiperrealista, sin música) + diálogo, terminando con
   "El start frame proporcionado define la apariencia del personaje, su ropa y el ambiente. Continúa desde ahí."

## Probar sin gastar créditos
`python tests/demo_server.py` (servicios simulados, puerto 8099) y `pytest -q` (las 6 fases de punta a punta).

## Estado real de la verificación
- Verificado: lógica completa, ffmpeg (escenas, recorte de silencios, subtítulos), interfaz y flujo de 6 fases con servicios **simulados**.
- **No verificado contra las APIs reales** (no había keys en este entorno): formato exacto de respuestas de Kie.ai (Veo, subida de imagen), ElevenLabs y Gemini. Si algo falla, el error sale en pantalla con el detalle y se corrige en `app/services/`.
- DubVoice (imagen `nano-banana-2` y video `veo-3.1-fast`, 9:16, imagen inicial en base64) está integrado como proveedor opcional, más barato. La ruta exacta para consultar el estado del video no está en su documentación pública: el adaptador prueba rutas conocidas; si falla, el error lo dice.
- Pexels: la key se detecta pero aún no se usa.
- Veo genera clips de 8 s: los diálogos se parten en tramos de ≤6.8 s y luego se recortan a lo hablado.

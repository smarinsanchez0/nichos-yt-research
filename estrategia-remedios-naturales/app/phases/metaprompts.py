"""Meta-prompts del metodo probado (guia_creacion_video_IA): Claude mira la CAPTURA de cada clip y escribe el prompt.

- META 1: imagen del primer clip (start frame) -> Nano Banana Pro con captura + avatar.
- META 2: imagenes siguientes -> Nano Banana Pro con Imagen A (captura, accion) + Imagen B (imagen anterior generada).
- META 3: video (Veo 3) -> start frame + prompt con dialogo.
"""
from __future__ import annotations

CLOSE_1 = "Use the provided character image for the person's appearance, face, and clothing exactly as shown."
CLOSE_2 = "Use Image A for the specific action and Image B for the character appearance and environment continuity."
CLOSE_3 = {"es": "El start frame proporcionado define la apariencia del personaje, su ropa y el ambiente. Continúa desde ahí.",
           "en": "The provided start frame defines the character's appearance, clothing and the environment. Continue from there."}

JSON_OUT = ('\n\nDevuelve UNICAMENTE JSON: {"image_prompt": "<el prompt final en ingles, listo para pegar, respetando TODAS las reglas>", '
            '"action": "<1-2 frases en ingles, empiezan con un verbo, SOLO accion y postura (manos, dedos, mirada, expresion, objetos)>"}')

META1 = """META-PROMPT 1 — IMAGEN PRIMER CLIP (Nano Banana Pro)
Analiza en detalle la imagen que te adjunto (captura de referencia de la escena que quiero recrear) y generame un prompt en ingles
optimizado para Nano Banana Pro.

Lo que tienes que analizar y describir:
  - Encuadre y composicion: angulo de camara, distancia, posicion del sujeto en el frame
  - Iluminacion: direccion, intensidad, temperatura de color, sombras
  - Fondo: descripcion detallada del entorno y todos los objetos presentes
  - Accion y postura: que esta haciendo el personaje, posicion del cuerpo (manos y dedos), direccion de la mirada, expresion facial
  - Objetos en escena: que sostiene, que hay en primer plano, que hay en el entorno (incluye graficos o ilustraciones fisicas)
  - Estilo visual y atmosfera general

Reglas criticas para el prompt que generes:
  - NUNCA describas la apariencia fisica, ropa, rasgos faciales ni ninguna caracteristica del personaje original. Esa informacion la
    aporta el avatar que cargo en Nano Banana Pro
  - Describe unicamente la postura, posicion y accion del personaje
  - Describe con maximo detalle todo lo que NO es el personaje: fondo, objetos, iluminacion, encuadre
  - Sin texto, logos, iconos, marcas, subtitulos ni overlays de ningun tipo (ignora los subtitulos que tenga la captura)
  - Usa lenguaje tecnico de fotografia y cinematografia
  - Formato vertical 9:16
  - Termina siempre el prompt con: "%s"
""" % CLOSE_1

META2 = """META-PROMPT 2 — IMAGEN CLIPS SIGUIENTES (Nano Banana Pro)
Analiza en detalle la imagen que te adjunto (captura de referencia del clip que quiero recrear) y generame un prompt en ingles
optimizado para Nano Banana Pro.

Contexto importante: en Nano Banana Pro voy a cargar DOS imagenes de referencia:
  - Imagen A: la captura del clip (accion a recrear)
  - Imagen B: la imagen generada del clip anterior (continuidad de personaje y ambiente)
Por eso el prompt debe enfocarse principalmente en la accion especifica de este clip. El personaje, su apariencia y el ambiente ya
estan garantizados por la Imagen B.

Lo que tienes que analizar y describir:
  - Encuadre y composicion: angulo de camara, distancia, posicion del sujeto en el frame
  - Accion y postura: que esta haciendo el personaje, posicion del cuerpo (manos y dedos), direccion de la mirada, expresion facial
  - Objetos en escena: que sostiene, con que interactua, que hay en primer plano (incluye graficos o ilustraciones fisicas)
  - Iluminacion: solo si hay algo muy especifico que destacar

Reglas criticas:
  - NUNCA describas apariencia fisica, ropa, rasgos faciales ni caracteristicas del personaje
  - NUNCA describas el fondo ni el ambiente en detalle, eso lo aporta la imagen anterior
  - Describe unicamente la accion, postura y objetos especificos de este clip
  - Sin texto, logos, iconos, marcas, subtitulos ni overlays (ignora los subtitulos que tenga la captura)
  - Usa lenguaje tecnico de fotografia y cinematografia
  - Formato vertical 9:16
  - Termina siempre con: "%s"
""" % CLOSE_2

META3_ES = """META-PROMPT 3 — VIDEO (Veo 3)
Tengo una captura de referencia de un clip y el texto que dice el personaje en ese momento. Analiza la captura y describe:
  - La accion exacta que esta haciendo el personaje (manos, dedos, mirada, expresion)
  - Los objetos con los que interactua (incluye graficos o ilustraciones fisicas)
  - El encuadre y composicion
NUNCA describas apariencia fisica, ropa, rasgos faciales ni el ambiente: el start frame ya tiene todo eso resuelto.
Escribe en %(lang)s. Devuelve UNICAMENTE JSON: {"camera": "encuadre/camara en una frase", "action": "accion y objetos, 1-3 frases, sin apariencia ni ambiente",
"action_es": "la accion resumida en español, una frase"}"""


def video_prompt(c: dict) -> str:
    """Prompt final de Veo 3 con las reglas fijas de la guia (el dialogo se inserta tal cual)."""
    es = c.get("lang") == "es"
    parts = [c.get("camera", "").strip(), c.get("action_en", "").strip()]
    if es:
        parts.append("Grabado con iPhone, cámara estática, sensación natural. Hiperrealista, movimientos humanos naturales, sin exageraciones.")
        if c.get("dialogue"):
            parts.append(f'El personaje realiza la acción y habla simultáneamente, diciendo en español: "{c["dialogue"]}"')
        else:
            parts.append("El personaje realiza la acción sin hablar.")
        parts.append("Sin música de fondo, sin efectos de sonido, solo sonido ambiente natural y la voz del personaje. "
                     "Sin texto, logos ni overlays superpuestos.")
    else:
        parts.append("Shot on iPhone, static camera, natural feel. Hyper-realistic, natural human movements, nothing exaggerated.")
        if c.get("dialogue"):
            parts.append(f'The character performs the action and speaks at the same time, saying: "{c["dialogue"]}"')
        else:
            parts.append("The character performs the action without speaking.")
        parts.append("No background music, no sound effects, only natural ambient sound and the character's voice. "
                     "No text, logos or overlays.")
    parts.append(CLOSE_3["es" if es else "en"])
    return " ".join(x for x in parts if x)


def ensure_close(prompt: str, close: str) -> str:
    prompt = prompt.strip()
    return prompt if prompt.endswith(close) else prompt + " " + close

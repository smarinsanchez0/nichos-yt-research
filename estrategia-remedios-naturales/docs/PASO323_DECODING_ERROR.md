# PASO 3.2.3 · DecodingError HTTP de DubVoice

**Causa raiz (reproducida en local, 0 red real):** `http.request_once` leia el cuerpo con `iter_bytes()` (httpx YA lo descomprime segun `Content-Encoding`)
y despues lo envolvia en `httpx.Response(..., headers=resp.headers, content=<bytes decodificados>)`. Ese constructor llama a `read()` y vuelve a
descomprimir esos bytes porque la cabecera `Content-Encoding` se conservaba -> `DecodingError: Error -3 ... incorrect header check`. Fallaba incluso con un
gzip/deflate VALIDO; `except httpx.HTTPError` lo convertia en `CONNECTION_ERROR ambiguous after_send`. El error es nuestro, no del servidor: la respuesta
de 106 s probablemente fue valida y el job se creo (inferencia: no se capturaron status/headers/cuerpo reales; `dubvoice_verified` sigue en false).

**Arreglo:** `request_once` lee bytes CRUDOS (`iter_raw()`), decodifica por su cuenta (`decode_body`: gzip, deflate zlib/raw; si el cuerpo es JSON plano con
un Content-Encoding falso se recupera y se anota) y devuelve un `WireResponse` (status, cabeceras originales, `raw`, `content_encoding`, `decode_note`,
`decode_error`). Nunca lanza por la decodificacion y jamas reenvia: es UNA peticion. Envia `Accept-Encoding: identity` (no se asume que el servidor lo respete;
el cliente es seguro si comprime igualmente; no pisa un valor explicito). `_veo_strict`: un 2xx con cuerpo no decodificable -> `INVALID_RESPONSE ambiguous
sub=undecodable_body` (misma politica: 1 POST, 0 reintentos, 0 fallback, NEEDS_REVIEW). El recorder guarda `content_encoding`, `request_accept_encoding`,
`raw_length`, `body_decode_note/error` y `raw_head_hex` (16 bytes); todo sanitizado.

**Creditos ≈15.000:** `summarize` suma TODO el proyecto, no solo el clip del canario; el clon conserva los `f5` de los demas clips (historial). 7.500 del
clip + 7.500 de otro intento pagado previo en la copia = 15.000. No hay doble conteo: el intento `legacy` se excluye (test). Se anade `credits_by_clip` y el log
final lo indica. Datos historicos sin tocar.

Tests: `tests/test_f5_http_encoding.py` (A-J + identity + causa raiz + resumen de creditos).

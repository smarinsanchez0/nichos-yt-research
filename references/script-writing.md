# Script-writing process — learned from Scripzy (Season 1, 2026-08-27)

This process was reverse-engineered from 10 real 10-minute scripts generated
with **Scripzy** (scripzy.app/script-lab, "Estilo Estándar"), using this
account's own credits, for the Temporada 1 topics. It documents both the
**style/structure to replicate** and a **critical, verified failure mode**
that makes a fact-check pass mandatory before any script is used — do not
skip straight to "generate and ship."

## Where the real scripts live

`~/Downloads/Temporada 1/` — all 10 full packages (guion + metadatos),
downloaded individually via Scripzy's "Descargar" button per script (the
bulk "Exportar ZIP" only exports the bare guion text, not the metadata —
confirmed by direct comparison: ZIP export was ~7-9K chars per file,
individual "Descargar" was ~10-13K chars with the same guion plus
everything below).

## The deliverable package (per video)

Every script comes as one document with these sections, in this order:

1. **GUION** — the ~1,200-1,600 word narration (10 min at Scripzy's
   pacing).
2. **METADATOS**
   - **DESCRIPCIÓN** — one emoji-heavy paragraph: hook question in caps,
     2-3 key science terms, an explicit caveat/disclaimer emoji line when
     the topic needs one (e.g. "⚠️ NOTA IMPORTANTE: EVIDENCIA LIMITADA EN
     HUMANOS"), a subscribe/bell CTA, 1-2 hashtags.
   - **ETIQUETAS** — ~25 comma-separated tags mixing broad terms (ciencia,
     salud, envejecimiento), specific terms (the video's actual mechanism —
     autofagia, mTOR, epigenética...), and 2-3 recurring **channel-brand
     tags** used on every video regardless of topic (for this channel:
     "futuro de la longevidad", "ciencia de la longevidad", "longevidad
     saludable").
   - **COMENTARIO FIJADO** — one short, emoji-heavy question mirroring the
     video's central question, designed to seed the comment section.
   - **TÍTULOS** — 10 numbered, ALL-CAPS bold title variants. Patterns
     that recur: "LO QUE NUNCA TE DIJERON SOBRE X", "¿PUEDE X HACER Y?",
     "LA VERDAD SOBRE X QUE NADIE TE CUENTA", "EL SECRETO DE X", "X: ¿MITO
     O CIENCIA REAL?". Same curiosity-over-accuracy logic as this skill's
     own §Title reframing — Scripzy independently converged on the same
     rule.
   - **FRASES PARA MINIATURA** — 7 short (4-8 word) punchy phrases for
     thumbnail text overlay, distinct from the titles (shorter, more
     visual/declarative: "¡LA CIENCIA REVELADA!", "¿CÓMO X AFECTA TUS
     CÉLULAS? ¡LA VERDAD!").
3. **In-app only, not in the exported .txt** — 4 detailed AI-image
   thumbnail prompts ("Variación 1-4"), each specifying composition,
   on-image text, font, and color scheme in English, e.g.: *"Close-up of a
   scientific illustration of cells undergoing autophagy, with the text
   'CÉLULAS LIMPIAS' in bold white with black outline, centered, using 3D
   extruded Impact font. The background is dark blue with glowing cellular
   structures."* A "Generar miniatura" button next to each can render the
   image directly (costs additional credits) — check in-app if the user
   wants the actual images, not just the prompts.

## The narration style (confirmed pattern across all 10 scripts)

1. **Hook**: an imaginative "Imagina que..." or a direct rhetorical
   question, never a flat statement of the topic.
2. **Real anchor**: a named scientist, year, or institution grounding the
   claim (Clive McCay/1930s for fasting, Dan Buettner/National Geographic
   for Blue Zones, Shinya Yamanaka for reprogramming) — concrete, not
   vague ("estudios demuestran que...").
3. **Explicit evidence-quality caveat early**, usually second paragraph:
   "Primero, es importante aclarar que..." — states plainly whether
   evidence is animal-only, correlational, small-sample, etc., before
   going further. This is the single most consistent structural habit
   across all 10 scripts and should never be dropped.
4. **Mechanism explained via one concrete analogy** — a city recycling
   old buildings for autophagy, a factory with a maintenance manual for
   cellular reprogramming, a highway network for muscle/energy transport.
   One analogy per mechanism, not stacked.
5. **A direct-address question to the viewer**, roughly two-thirds
   through, inviting a comment — not saved only for the very end.
6. **Explicit disclaimer paragraph** for anything health-adjacent: "esta
   información es de carácter científico y no constituye consejo médico."
   Non-negotiable inclusion whenever the topic touches diet, fasting,
   supplements, or medical conditions.
7. **Widens to future research / open questions** in the back third,
   rather than ending right after the mechanism — genetic variability,
   ethical debates, long-term study design, biomarkers.
8. **Philosophical closing beat** — reframes "living longer" as "living
   well," not just more years, before the final CTA.
9. **Standard sign-off CTA**: like, share, subscribe, bell notification,
   "nos vemos en el próximo capítulo/video."

## Non-negotiable: fact-check every number before use

**Verified real failure, 2026-08-27**: Video 1's script (reprogramación
celular / ER-100) stated glaucoma affects *"más de dos mil millones de
personas en todo el planeta"* — independently checked via WebSearch
against the actual epidemiological literature (Tham et al., Ophthalmology
2014; Glaucoma Research Foundation): the real figure is **~80 million**
(60 million specifically for open-angle glaucoma). Scripzy invented a
number roughly **25x too large**, despite the brief explicitly instructing
"no inventar cifras específicas." This happened even though the rest of
that script's science (ER-100, Yamanaka factors, the specific eye
conditions, the FDA timeline) was accurate and matched what this skill had
independently verified via WebSearch earlier that same session.

**Conclusion: never trust a Scripzy-generated statistic as fact.** Before
a script goes into production (voiceover, upload), every concrete number
in it — prevalence figures, percentages, dates, named studies — must be
independently re-verified with WebSearch/WebFetch, exactly like this
skill's own §Research pipeline already requires for everything else. Where
a number can't be verified quickly, cut it or replace it with a qualitative
statement ("una de las causas más comunes de discapacidad visual") rather
than a specific figure.

## Practical notes for reusing Scripzy

- Script Lab flow: Nuevo Guion → **Información Básica** (Nombre del
  Proyecto = internal label only; Idea Central = the actual detailed brief
  — title, format, tone, topic, retention arc, explicit accuracy
  constraints) → **Configuración** (duración already maxes at 10 min /
  ~7,500 characters per script on this account's tier; Idioma Español) →
  **Estilo** (Estándar = the account's tuned viral-engagement style,
  already good — no need for "Personalizado" unless deliberately imitating
  a specific reference video) → **Revisión** → **Generar Guion** (~7,500
  credits per script; this account started at 300K credits, i.e. room for
  ~40 scripts).
- The longer/more specific the Idea Central brief, the more the output
  respects real constraints (e.g. explicitly naming the exact real trial
  and disease it treats kept the whole script accurate on that point) —
  short vague briefs invite more invented specifics.
- Bulk "Exportar ZIP" (top of the Guiones Generados / history view) is
  fast but guion-only. For the full package (titles/tags/description/
  pinned comment/thumbnail phrases), open each script and use its own
  "Descargar" button — one extra click per script, no extra credits.
- Generated scripts only persist 30 days in Scripzy's history — export/
  download promptly, don't rely on it as long-term storage.

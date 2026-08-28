---
name: NICHOS
description: Faceless-YouTube-channel niche, content-strategy, and channel-growth research assistant, grounded entirely in real, freshly-fetched data — never invented numbers. Use when the user wants a niche/subnicho/micronicho for a faceless channel, wants their real channel (DescubrimientosFascinantes) or a competitor analyzed, wants a "temporada 1" of video topics predicted from real performance data, wants progress toward the 4,000-watch-hour/1,000-subscriber YouTube Partner Program threshold tracked, or wants current research on YouTube algorithm/monetization/niche strategy (forums, creator blogs, books, video transcripts). Every run pulls live data first — the channel's real YouTube Studio analytics via the connected browser, plus at least one fresh web search/fetch — and logs sources to references/knowledge-base.md before producing any recommendation. Distinct from every other skill on this account (AURA, EL-HERRERO, AUTOSOFT, viral-motion-editor, talking-parts-storyteller): those all edit/render video for existing physical-product brands (El Mecánico, El Herrero); NICHOS never touches Remotion or renders anything — its only output is research-backed written strategy. Not for ad copy on an existing tested brand (that's AURA) and not for editing supplied footage (that's viral-motion-editor).
---

# NICHOS — faceless YouTube channel niche & growth research assistant

A research/ideation skill for growing **DescubrimientosFascinantes**
(`youtube.com/@DescubrimientosFascinantes`, branded "Futuro de la
Longevidad") toward YouTube Partner Program eligibility — 1,000
subscribers and 4,000 public watch hours in a trailing 12 months — and,
more generally, for finding/validating niches for any **faceless** YouTube
channel (no on-camera host — animation, screen recording, stock/cinematic
b-roll, graphics, voiceover).

**The one rule everything else depends on: never invent a number.**
Every stat this skill states (views, watch hours, CTR, retention, RPM,
competitor size, "trending" claims) must come from something actually
fetched in that session — the connected browser reading YouTube
Studio/the public channel, a WebSearch/WebFetch result, or a source the
user pasted in. If live data isn't reachable, say so explicitly and work
from the last dated snapshot in `references/channel-snapshot.md` — labeled
as such, not presented as current. Pure text/research output — no editing,
no rendering. If the user already has a topic picked and wants the actual
video produced, hand off to `viral-motion-editor` (or the brand-specific
skill, if one exists) once the topic/script is decided here.

## Research pipeline — real data, every run

Before proposing anything, gather real data. This is not optional prep —
it's the thing that makes this skill different from a generic brainstorm:

1. **Refresh the channel snapshot.** If the connected browser (Claude in
   Chrome) is available, navigate to
   `studio.youtube.com/channel/UCT3TlG-UaoUEzNyfR3_dwuw/analytics` and read
   the real Resumen (views/watch-hours/subscribers, last 28d and last 365d)
   and Contenido (per-video views, avg. view duration %, CTR) tabs. Update
   `references/channel-snapshot.md` with the new numbers and today's date —
   don't just read it silently, overwrite the stale snapshot so the next
   run starts from current data. If the browser isn't reachable, say so
   out loud and use the existing snapshot, clearly labeled with its
   capture date (e.g. "según el último dato del 27 ago 2026").
2. **Check the public channel** (`youtube.com/@DescubrimientosFascinantes`
   and `/videos`) for anything Studio doesn't show as clearly — new
   uploads, thumbnail/title changes, view counts on individual videos.
3. **Do at least one real web search or fetch** on something concrete:
   current YouTube algorithm/monetization behavior, a named competitor
   channel, a specific forum thread (r/NewTubers, r/PartnerProgram), a
   creator's blog post, or a book/video on niche strategy the user points
   to. Use `WebSearch`/`WebFetch` — never answer a "what's working right
   now" question from memory alone, since this space changes constantly.
4. **Log it.** Append a dated entry to `references/knowledge-base.md`
   (query, sources with real URLs, findings, how it changed the
   recommendation) before writing the final answer — per the template at
   the bottom of that file. This is what lets the skill accumulate real
   expertise across sessions instead of re-researching from zero, and
   what the daily scheduled run (see §Daily operation) depends on to show
   its work.
5. **If the user hands over a video, article, or PDF** that teaches YouTube
   strategy (a mentor's video, a book chapter, a forum thread) — actually
   read/transcribe it, summarize the real content into
   `references/knowledge-base.md` with attribution, and reference it in the
   recommendation. Never fabricate what a named creator "teaches" — only
   summarize material actually supplied or fetched.

## Daily operation — working toward 4,000 hours

The user's explicit goal for `DescubrimientosFascinantes` is monetization:
1,000 subscribers (514 as of the last snapshot) and 4,000 watch hours in a
trailing 12 months (292.5 as of the last snapshot — see
`references/channel-snapshot.md`). Every invocation of this skill,
scheduled or manual, should move that forward concretely, not just produce
ideas in the abstract:

- Restate current progress toward both thresholds using the freshest
  snapshot available (§Research pipeline step 1), and how much moved since
  the last logged snapshot.
- Produce at least one concrete, immediately actionable output per run —
  a scored topic, a title reframe, a retention-arc note on an underperformer,
  or a flag on something in §Key finding (channel-snapshot.md) that needs a
  decision (e.g. the course-sale videos outperforming the branded niche).
- When running unattended (a scheduled daily task, no live browser
  session): do §Research pipeline steps 3-4 only (web research — this part
  doesn't need the browser), and clearly mark the channel-progress numbers
  as carried over from the last manual snapshot rather than re-checked.
  Flag to the user when the snapshot is getting stale (more than ~7 days
  old) so they know to run it manually with the browser open for a real
  refresh.

## The core method: drill down until you hit a winning angle

Never stop at a broad topic. Walk it down four levels:

**Nicho → Subnicho → Micronicho → Submicronicho**

Example: `Ciencia → Historias de supervivencia → Supervivencias explicadas
científicamente → Personas que sobrevivieron accidentes extremos gracias a
fenómenos físicos inesperados` → video: *"Cayó desde miles de metros y
sobrevivió: la física que lo hizo posible."*

A submicronicho only counts as a **winning angle** if it scores on all four
of:

- **Demanda** — people are actually searching/watching this topic.
- **Curiosidad** — the title asks a question the viewer needs answered.
- **Producción faceless** — fully buildable with animation, screen capture,
  stock footage, graphics, or narration — never a face on camera.
- **Diferenciación** — specific enough that it doesn't collapse back into
  "just another [broad topic] channel."

Stopping one level too early (e.g. shipping "Ciencia" or "Salud" as the
channel identity) is the most common failure — it reads as generic and
doesn't tell YouTube's algorithm who to show the channel to.

## Seed categories (starting points, not a fixed menu)

Use these as prompts to kick off the drill-down when the user has no
starting idea, not as an exhaustive list:

- 💰 Finanzas y dinero → historias financieras → errores financieros → casos de quiebras reales
- 🤖 IA y tecnología → herramientas de IA → automatización → IA aplicada a una profesión concreta
- 🧠 Psicología → comportamiento laboral → manipulación → dinámicas específicas de oficina
- 🕵️ Misterio → casos reales → misterios tecnológicos → enigmas de Internet
- 🔬 Ciencia → supervivencia científica → fenómenos extremos → explicación de un evento extraordinario concreto
- 🏗️ Ingeniería → catástrofes → fallos estructurales → análisis de un accidente concreto
- 📜 Historia → errores históricos → consecuencias inesperadas → un acontecimiento poco conocido
- 🦴 Animales → especies extintas → criaturas poco conocidas → comportamiento y evolución
- ⚡ Productividad → neurociencia → hábitos → un mecanismo concreto de concentración
- 🎬 Entretenimiento → análisis de personajes → villanos → psicología de un personaje

## Niche scorecard

Score every serious candidate niche (not just the final pick) on these
dimensions, 1-10 stars each, plus a **Potencial global** average:

| Factor | What it measures |
|---|---|
| Demanda | real search/watch interest today |
| Viralidad | how strong the hook/curiosity pull is |
| Evergreen | does it stay relevant in 6-12 months, or expire fast |
| Faceless | how easily it's produced with no on-camera host |
| Facilidad de producción | b-roll/stock/animation availability, script complexity |
| CPM/RPM potencial | rough ad-revenue ceiling — see §Category economics |
| Competencia | how saturated the exact submicronicho already is |
| Potencial global | overall call |

## Category economics (use as judgment, not fabricated stats)

Advertiser demand is not flat across categories: topics like education,
finance, tech, and health/science tend to command meaningfully higher RPM
than general entertainment, food, or lifestyle content — but the actual
number for any given channel depends heavily on audience geography,
retention, video length, and format. Never quote a specific RPM/CPM figure
as fact unless the user supplies real data (their own AdSense numbers, a
named study they trust) — otherwise talk in relative terms ("this category
tends to monetize better than X") and say so explicitly.

## Season 1, not scattered videos

Once a niche is picked, never hand back a random list of unconnected video
ideas. Propose a first **season** — 8-10 topics that together read as one
coherent universe, so a new viewer and YouTube's own recommendation system
both understand what the channel is about within the first few uploads.
For each topic give: title, viralidad score, CPM-potencial score, and a
one-line reason it belongs in this season specifically (not just "it's
popular"). Then give a suggested **publish order** — lead with the topic
that has the strongest hook + real news/momentum behind it, not necessarily
video #1 on the list.

For the top 1-3 videos in the season, go one level deeper:

- **Hook** — 2-3 sentences the narration would open with.
- **Thumbnail concept** — e.g. split-image before/after, bold 2-4 word
  overlay text, no stock-photo genericness.
- **Retention arc** — a repeatable narrative signature so the channel
  doesn't read as "news recap." A general-purpose one: **Descubrimiento**
  (what did they find) → **Misterio** (why does it matter) → **Ciencia/
  Mecanismo** (how does it work) → **Prueba/Experimento** (what actually
  happened) → **Límite** (what's still unknown) → **Futuro** (what could
  happen next). Adapt the labels to the niche (history/psychology/true-crime
  niches need their own equivalent arc) but keep the same idea: a repeatable
  structure that drives retention, not just a one-off clickbait hook.

## Title reframing: curiosity beats accuracy

Academic/accurate phrasing kills CTR. Always reframe the literal fact into
a question or a specific concrete image:

- ❌ "Beneficios nutricionales de las legumbres" → 🔥 "El alimento barato
  que las personas longevas comen constantemente"
- ❌ "Mecanismos moleculares de la senescencia celular" → 🔥 "Las células
  que se niegan a morir podrían estar envejeciendo tu cuerpo"

The underlying fact must stay accurate — only the framing changes.

## Shorts funnel

Every long video seeds 2-3 Shorts, and the Shorts are not random clips —
each one poses a smaller piece of the same question and points back at the
long video for the answer. Example: long video "Los científicos están
intentando reiniciar nuestras células" → Short 1 "¿Sabías que una célula
puede tener una edad biológica?" → Short 2 "El experimento que intenta
rejuvenecer células" → Short 3 "¿Podríamos algún día rejuvenecer nuestro
cuerpo?".

## Content pillars (for hybrid niches)

If two adjacent submicronichos both score well, don't force a choice —
propose a channel with pillars and a percentage split (e.g. 40% ciencia del
descubrimiento / 35% aplicación práctica / 25% futuro-tecnología), so early
videos can test which pillar performs best before committing further.

## Script writing — once a topic is chosen

When the user wants an actual narration script for a Season 1 (or any)
topic, follow `references/script-writing.md` in full — it documents the
real structure/style/deliverable-package pattern reverse-engineered from
10 real scripts generated for this channel via **Scripzy**
(scripzy.app/script-lab, this account's own paid tool, credits already
available). Short version:

- **Structure**: hook → named real anchor (scientist/year/institution) →
  explicit evidence-quality caveat early ("primero, es importante
  aclarar...") → one concrete analogy per mechanism → mid-video direct
  question to the viewer → explicit non-medical-advice disclaimer for any
  health-adjacent claim → widen to future research/open questions → a
  philosophical closing beat → standard like/subscribe/bell sign-off.
- **Deliverable package per video**: Guion + Descripción (emoji-heavy,
  hook + disclaimer + hashtags) + ~25 Etiquetas (broad + specific +
  recurring channel-brand tags) + Comentario fijado + 10 título variants
  (ALL-CAPS, curiosity-framed) + 7 frases para miniatura + (in Scripzy's
  UI only) 4 detailed AI thumbnail-image prompts.
- **Mandatory fact-check pass, no exceptions**: a real, verified example
  from this channel's own Season 1 — Scripzy's Video 1 script stated
  glaucoma affects "más de dos mil millones de personas" when the real,
  WebSearch-verified figure is ~80 million (a ~25x invention), despite the
  brief explicitly saying not to invent figures. **Every concrete number
  in any generated script must be independently re-verified with
  WebSearch/WebFetch before the script is used for anything** — same
  standard as this skill's own §Research pipeline. Cut or qualitatively
  soften any number that can't be quickly verified.
- If using Scripzy again: Nuevo Guion → Información Básica (a *long,
  specific* Idea Central brief — the more precisely it names the real
  facts/constraints, the less Scripzy invents) → Configuración (10 min /
  Español) → Estilo Estándar → Generar. Bulk "Exportar ZIP" is guion-only;
  use each script's own "Descargar" button for the full package. History
  retains scripts only 30 days — export promptly.

## Monetization: a fast digital product, not a course

Once the niche and Season 1 are set, propose one low-effort, low-ticket
digital product that matches the niche — never a full multi-hour course as
the first product:

- **Format:** PDF/ebook 40-60 pages + a checklist + a simple tracker
  template.
- **Price:** $9-19.
- **Funnel:** video → free lead magnet (a narrower slice of the paid
  product, offered in the description) → email capture → paid upsell. Build
  the audience with content first; don't try to sell from video 1.
- **Claims discipline:** in health, finance, or any regulated-adjacent
  niche, position the product as an educational guide grounded in current
  understanding — never as medical/financial advice or a guaranteed
  outcome. Flag this explicitly to the user when the niche touches health
  or money.

## Pipeline

1. **Research first** — run §Research pipeline in full (refresh snapshot,
   check public channel, real search/fetch, log to knowledge-base.md).
   Never skip to ideation on a cold start.
2. **Get the starting point** — an interest, the existing channel (default
   subject unless told otherwise), or nothing (use §Seed categories to
   prompt).
3. **Drill down** Nicho → Subnicho → Micronicho → Submicronicho, testing
   each candidate against demanda + curiosidad + producción faceless +
   diferenciación — grounded in what step 1 actually found, not
   assumptions.
4. **Score** every serious candidate with the §Niche scorecard.
5. **Propose Season 1** — 8-10 titles, scored, with a publish order and the
   reasoning for why they form one universe.
6. **Deep-dive** the top 1-3 videos — hook, thumbnail concept, retention
   arc.
7. **Reframe** any academic-sounding title per §Title reframing.
8. **Propose the Shorts funnel** for the top video(s).
9. **Propose content pillars** with a % split if the niche is hybrid.
10. **Propose the monetization product** — format, price, funnel, claims
    caveat if relevant.
11. **Report progress toward 4,000 hours / 1,000 subs** per §Daily
    operation.
12. **If a script is requested**, follow §Script writing / 
    `references/script-writing.md` — including the mandatory fact-check
    pass on every number before delivering it.
13. Deliver as one structured write-up (tables for scores/season list). If
    it's long and the user will want to revisit or share it, offer to
    publish it as an Artifact instead of leaving it only in chat.

## Reference index

| File | Covers |
|---|---|
| `references/channel-snapshot.md` | The real, dated DescubrimientosFascinantes data pull: subscriber/watch-hour progress toward YPP, per-video views/CTR/retention, the full upload list, and the "top performers aren't the branded niche" finding. Overwrite with fresh numbers every time Studio is actually checked. |
| `references/knowledge-base.md` | Dated log of every real source this skill has actually fetched/read — blogs, forum threads, studies, supplied videos/books — with URLs and findings. Append-only; never invent an entry. |
| `references/script-writing.md` | The narration structure/style and full deliverable-package pattern learned from 10 real Scripzy-generated scripts, plus the verified hallucinated-statistic finding that makes a fact-check pass mandatory before any script ships. |

## Non-negotiables

- Never state a number (views, watch hours, CTR, RPM, competitor size,
  "trending now") that wasn't actually fetched this session or pulled from
  a dated snapshot labeled as such — no estimates presented as fact.
- Never skip §Research pipeline on a cold start — at minimum one real web
  search/fetch, logged, before recommending anything.
- Never recommend an on-camera/face-showing format — every idea must be
  produceable faceless.
- Never lead with a full course as the monetization product — always the
  fast low-ticket digital product first.
- Never hand back scattered, unconnected video ideas — always frame as a
  season/universe with a publish order.
- Never inflate or fabricate claims in health/finance-adjacent niches —
  flag the "educational, not advice" framing explicitly when it applies.
- Never present the branded-niche pivot as already working when the real
  channel data says otherwise (see channel-snapshot.md §Key finding) —
  surface the tension, don't paper over it.
- Never deliver a generated script (Scripzy or otherwise) without
  independently fact-checking every concrete number in it first — verified
  real case: a ~25x hallucinated statistic slipped through despite an
  explicit "don't invent figures" instruction (see
  `references/script-writing.md`).

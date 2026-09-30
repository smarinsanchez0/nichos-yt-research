/* ESTRATEGIA REMEDIOS NATURALES - interfaz (sin dependencias) */
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const S = { pid: localStorage.pid || null, p: null, tab: +(localStorage.tab || 1), voices: null, sig: '' };

async function api(method, url, body, isForm) {
  const opt = { method, headers: {} };
  if (body !== undefined) { if (isForm) opt.body = body; else { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; } }
  const r = await fetch(url, opt);
  let data = null; try { data = await r.json(); } catch (e) {}
  if (!r.ok) { const m = (data && data.detail) || r.statusText; toast(typeof m === 'string' ? m : JSON.stringify(m), true); throw new Error(m); }
  return data;
}
function toast(msg, err) {
  const d = document.createElement('div'); if (err) d.className = 'e'; d.textContent = msg;
  $('#toast').appendChild(d); setTimeout(() => d.remove(), err ? 9000 : 3500);
}
const file = (path) => `/files/${S.pid}/${path}`;
const job = (name) => (S.p?.jobs || {})[name];
const running = (name) => job(name)?.status === 'running';
const anyRunning = () => Object.values(S.p?.jobs || {}).some(j => j.status === 'running');

function jobBox(name, label) {
  const j = job(name); if (!j) return '';
  if (j.status === 'running') return `<div class="job"><div class="msg">⏳ ${esc(label || name)}: ${esc(j.message || '')}</div><div class="bar"><i style="width:${Math.round((j.progress || 0.03) * 100)}%"></i></div></div>`;
  if (j.status === 'done' && (j.message || '').startsWith('ℹ️')) return `<div class="job"><div class="msg">${esc(j.message)}</div></div>`;
  if (j.status === 'error') return `<div class="job error"><b>❌ ${esc(label || name)} falló.</b><br>${esc(j.error)}</div>`;
  return '';
}

/* ------------------------------------------------------------- estado de fases */
function phaseInfo(p) {
  const pts = Object.values(p.analysis.points).filter(Boolean).length;
  const sc = p.scenes, appr = sc.filter(s => s.image?.approved).length, withImg = sc.filter(s => s.image).length;
  const clips = sc.flatMap(s => s.clips || []), done = clips.filter(c => c.status === 'done' && !c.stale).length;
  return [
    { n: 1, t: 'Avatar', d: !!p.avatar?.profile, sub: p.avatar?.profile ? 'listo' : (p.avatar ? 'analizando…' : 'sube la foto'), pr: p.avatar?.profile ? 1 : 0 },
    { n: 2, t: 'Análisis del video', d: pts === 4, sub: `${pts}/4 puntos`, pr: pts / 4 },
    { n: 3, t: 'Imágenes', d: sc.length > 0 && appr === sc.length, sub: sc.length ? `${appr}/${sc.length} aprobadas` : '—', pr: sc.length ? appr / sc.length : 0 },
    { n: 4, t: 'Guion + prompts video', d: sc.length > 0 && sc.every(s => s.clips?.length), sub: sc.every(s => s.clips?.length) && sc.length ? `${clips.length} clips` : '—', pr: sc.length && sc.every(s => s.clips?.length) ? 1 : 0 },
    { n: 5, t: 'Videos IA', d: clips.length > 0 && done === clips.length, sub: clips.length ? `${done}/${clips.length} clips` : '—', pr: clips.length ? done / clips.length : 0 },
    { n: 6, t: 'Edición final', d: !!p.final, sub: p.final ? 'exportado' : '—', pr: p.final ? 1 : 0 },
  ];
}

/* ------------------------------------------------------------- render */
function render() {
  const p = S.p; if (!p) return;
  const info = phaseInfo(p);
  $('#stepper').innerHTML = info.map(i => `<div class="step ${i.d ? 'done' : ''} ${i.n === S.tab ? 'active' : ''}" data-tab="${i.n}"><b>FASE ${i.n} ${i.d ? '✓' : ''}</b><span>${i.t}</span><div class="muted" style="font-size:11px">${i.sub}</div><div class="bar"><i style="width:${Math.round(i.pr * 100)}%"></i></div></div>`).join('');
  const views = { 1: v1, 2: v2, 3: v3, 4: v4, 5: v5, 6: v6 };
  if (S.tab === 5) api('GET', `/api/projects/${S.pid}/videos/estimate`).then(e => { const el = $('#est'); if (el) el.innerHTML = e.variable ? `💳 Estimado DubVoice: <b>${e.total_credits.toLocaleString()}</b> créditos con duración variable (vs ${e.veo_fast_credits.toLocaleString()} con Veo fast de 8 s fijos)` : (true ? `💳 Estimado: <b>${e.total_credits.toLocaleString()}</b> créditos (este modelo siempre genera 8 s; elige omniflash para duración por clip)` : 'Veo (Kie.ai) siempre genera 8 s por clip; se recorta en la edición.'); }).catch(() => {});
  $('#main').innerHTML = `<div class="card row"><div class="grow"><label>Nombre del proyecto</label><input data-setting="name" value="${esc(p.name)}"></div><div><label>&nbsp;</label><button class="ghost danger" data-act="delProj">Borrar proyecto</button></div></div>` + views[S.tab](p);
}

function v1(p) {
  const a = p.avatar, pr = a?.profile;
  return `<h2>Fase 1 · Avatar de IA</h2><p class="muted">Sube una foto clara del personaje (rostro visible). Será el protagonista de todo el video.</p>
  <div class="card row"><div style="width:220px">${a ? `<img src="${file(a.file)}?t=${a.w}${a.h}" class="thumb">` : '<div class="thumb" style="display:grid;place-items:center" >sin foto</div>'}</div>
  <div class="grow"><input type="file" id="avatarFile" accept="image/*"> <button data-act="uploadAvatar">Subir avatar</button>
  ${jobBox('avatar', 'Análisis del avatar')}
  ${pr ? `<h3>Perfil detectado</h3><label>Descripción para prompts (inglés, editable)</label><textarea data-profile="description">${esc(pr.description)}</textarea>
  <div class="row"><div class="grow"><label>Género de voz</label><select data-profile-voice="gender"><option ${pr.voice?.gender === 'male' ? 'selected' : ''} value="male">Hombre</option><option ${pr.voice?.gender === 'female' ? 'selected' : ''} value="female">Mujer</option></select></div>
  <div class="grow"><label>Edad de la voz</label><select data-profile-voice="age">${['young', 'middle_aged', 'old'].map(x => `<option ${pr.voice?.age === x ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
  <div class="grow"><label>Tono</label><input data-profile-voice="tone" value="${esc(pr.voice?.tone)}"></div></div>
  <p class="muted">${esc(pr.summary_es || '')}</p><button class="ghost" data-act="reAvatar">Re-analizar</button>
  <p><button data-goto="2">Continuar a la Fase 2 →</button></p>` : ''}</div></div>`;
}

function v2(p) {
  const st = p.settings, an = p.analysis, src = p.source, pts = an.points;
  const names = { transcription: 'Transcripción', translation: 'Traducción al español', scenes: 'Escenas y frames leídos', prompts: 'Prompts de imagen' };
  const tr = an.translation?.segments || [];
  return `<h2>Fase 2 · Análisis del video original</h2><p class="muted">Sube el video en inglés a replicar. La app lo transcribe, lo traduce, detecta cada escena, lee los frames y redacta un prompt de imagen por escena.</p>
  <div class="card row"><div class="grow"><input type="file" id="videoFile" accept="video/*"> <button data-act="uploadVideo">Subir video</button>
  ${src ? `<p class="muted">${esc(src.name)} · ${src.duration.toFixed(1)}s · ${src.width}×${src.height} ${src.has_audio ? '' : '· ⚠ sin audio'}</p>` : ''}
  <div class="row"><div class="grow"><label>Sensibilidad de corte de escena (menor = más escenas): <b>${st.scene_threshold}</b></label><input type="range" min="0.1" max="0.6" step="0.05" value="${st.scene_threshold}" data-setting="scene_threshold" data-num="1"></div>
  <div class="grow"><label>Duración máx. por escena (s)</label><input type="number" min="3" max="8" step="0.5" value="${st.max_scene_len}" data-setting="max_scene_len" data-num="1"></div>
  <div class="grow"><label>Idioma del video final</label><select data-setting="output_language"><option value="es" ${st.output_language === 'es' ? 'selected' : ''}>Español (el avatar habla en español)</option><option value="en" ${st.output_language === 'en' ? 'selected' : ''}>Inglés</option></select></div>
  <div class="grow"><label>Transcripción</label><select data-setting="stt_provider"><option value="local" ${st.stt_provider === 'local' ? 'selected' : ''}>Local en tu Mac (gratis)</option><option value="elevenlabs" ${st.stt_provider === 'elevenlabs' ? 'selected' : ''}>ElevenLabs Scribe</option></select></div>
  <div class="grow"><label>Modelo de Claude</label><input value="${esc(st.claude_model)}" data-setting="claude_model"></div></div>
  <label>Notas globales para todas las escenas (ropa, estilo, banderas, lo que quieras forzar)</label><textarea data-setting="global_notes" placeholder="Ej: siempre con la bandera de EE.UU. a la izquierda; cocina luminosa estilo americano.">${esc(st.global_notes)}</textarea>
  <p><button data-act="analyze" ${!src || running('analysis') ? 'disabled' : ''}>▶ Analizar video</button> <button class="ghost" data-act="reanalyze" ${!src || running('analysis') ? 'disabled' : ''}>Rehacer todo desde cero</button> <button class="ghost" data-act="redoPrompts" ${!src || running('analysis') ? 'disabled' : ''}>Rehacer solo prompts de imagen</button> <button class="ghost" data-act="redoScenes" ${!src || running('analysis') ? 'disabled' : ''}>Rehacer solo escenas + prompts</button></p>
  ${jobBox('analysis', 'Análisis')}</div>
  <div style="width:220px">${src ? `<video src="${file(src.file)}" controls class="thumb"></video>` : ''}</div></div>
  <div class="pts">${Object.keys(names).map(k => `<div class="pt ${pts[k] ? 'ok' : ''}"><span class="dot"></span><b>${names[k]}</b><div class="muted">${pts[k] ? '+1 punto' : 'pendiente'}</div></div>`).join('')}</div>
  ${an.transcript ? `<div class="cols"><div class="card"><h3>Transcripción (${esc(an.transcript.language)})</h3><div class="scroll">${(tr.length ? tr : [{ en: an.transcript.text, start: 0 }]).map(s => `<div class="seg"><div class="t">${s.start.toFixed(1)}s</div>${esc(s.en)}</div>`).join('')}</div></div>
  <div class="card"><h3>Traducción al español</h3><div class="scroll">${tr.filter(s => s.es).map(s => `<div class="seg"><div class="t">${s.start.toFixed(1)}s</div>${esc(s.es)}</div>`).join('') || '<span class="muted">pendiente</span>'}</div></div></div>` : ''}
  ${p.scenes.length ? `<h3>${p.scenes.length} escenas detectadas</h3><div class="grid">${p.scenes.map(s => `<div class="card"><div class="row" style="flex-wrap:nowrap"><img class="thumb" style="width:120px" src="${file(s.frame)}"><div class="grow"><b>Escena ${s.idx + 1}</b> <span class="tag">${s.start.toFixed(1)}–${s.end.toFixed(1)}s</span>
  <div class="muted">${esc(s.read?.summary_es)}</div></div></div>
  <details><summary>Lectura del frame</summary><small>${['shot', 'person', 'setting', 'lighting', 'motion'].map(k => `<b>${k}:</b> ${esc(s.read?.[k])}`).join('<br>')}</small></details>
  <label>Diálogo (EN)</label><div>${esc(s.dialogue_en) || '<span class="muted">— sin diálogo —</span>'}</div>${s.dialogue_es ? `<label>Diálogo (ES)</label><div class="muted">${esc(s.dialogue_es)}</div>` : ''}
  <label>Prompt de imagen (editable)</label><textarea data-scene="${s.idx}" data-field="image_prompt">${esc(s.image_prompt)}</textarea></div>`).join('')}</div>` : ''}
  ${pts.prompts ? '<p><button data-goto="3">Continuar a la Fase 3 →</button></p>' : ''}`;
}

function v3(p) {
  const sc = p.scenes, st = p.settings;
  if (!sc.length || !p.analysis.points.prompts) return `<h2>Fase 3 · Imágenes</h2><p class="muted">Completa primero la Fase 2 (4 puntos).</p>`;
  const appr = sc.filter(s => s.image?.approved).length;
  return `<h2>Fase 3 · Imágenes del avatar por escena</h2><p class="muted">Nano Banana genera al avatar en la misma pose y decorado de cada escena original. Retoca con un prompt cualquier imagen que no te convenza y aprueba las buenas.</p>
  <div class="card row"><div><label>Proveedor de imagen</label><select data-setting="image_provider"><option value="google" ${st.image_provider === 'google' ? 'selected' : ''}>Google AI Studio (directo)</option><option value="dubvoice" ${st.image_provider === 'dubvoice' ? 'selected' : ''}>DubVoice.ai (más barato)</option><option value="kie" ${st.image_provider === 'kie' ? 'selected' : ''}>Kie.ai</option></select></div>
  <div class="grow"><label>Modelo (Google) · Nano Banana: gemini-2.5-flash-image (mejor precio) · gemini-3-pro-image-preview (máxima calidad)</label><input data-setting="image_model" value="${esc(st.image_model)}"></div>
  <div class="grow"><label>Modelo (Kie.ai)</label><input data-setting="kie_image_model" value="${esc(st.kie_image_model)}"></div><div class="grow"><label>Modelo (DubVoice)</label><select data-setting="dubvoice_image_model">${[['nano-banana-pro','Nano Banana Pro · 3.500 cr (mejor consistencia)'],['nano-banana-2','Nano Banana 2 · 1.000 cr'],['nano-banana-2-lite','Nano Banana 2 Lite · 500 cr']].map(([v,t]) => `<option value="${v}" ${st.dubvoice_image_model === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
  <div class="grow"><label>Referencia de la escena original</label><select data-setting="scene_ref_mode"><option value="guide" ${st.scene_ref_mode === 'guide' ? 'selected' : ''}>Método de la guía (recomendado): cada imagen usa la anterior como referencia</option><option value="swap" ${st.scene_ref_mode === 'swap' ? 'selected' : ''}>Reemplazar persona sobre el frame</option><option value="blur" ${st.scene_ref_mode === 'blur' ? 'selected' : ''}>Difuminada (no copia a la persona, pose aproximada)</option><option value="none" ${st.scene_ref_mode === 'none' ? 'selected' : ''}>Solo texto (máxima fidelidad al avatar)</option><option value="full" ${st.scene_ref_mode === 'full' ? 'selected' : ''}>Completa (copia más el frame, arriesga copiar a la persona)</option></select></div>
  <div class="grow"><label class="inline"><input type="checkbox" data-setting="image_qa" data-bool="1" ${st.image_qa ? 'checked' : ''}> Revisión automática con Claude (corrige hasta 2 veces si no es tu avatar o no hace la acción)</label></div>
  <div class="grow"><label class="inline"><input type="checkbox" data-setting="image_fallback" data-bool="1" ${st.image_fallback ? 'checked' : ''}> Si falla, usar otro proveedor automáticamente</label></div></div>
  <p><button data-act="genImages" ${running('images') ? 'disabled' : ''}>🎨 Generar imágenes faltantes</button> <button class="ghost" data-act="regenAll" ${running('images') ? 'disabled' : ''}>♻️ Regenerar TODAS desde cero</button> <button class="ghost" data-act="approveAll">Aprobar todas</button> <b>${appr}/${sc.length} aprobadas</b>${appr === sc.length ? ' <button data-goto="4">Continuar a la Fase 4 →</button>' : ''}</p>
  ${jobBox('images', 'Generación de imágenes')}${running('images') ? '<p><button class="danger" data-act="resetJob" data-job="images">⛔ Destrabar / detener espera</button></p>' : ''}
  ${sc.map(s => { const im = s.image, jn = `img:${s.idx}`; return `<div class="card ${im?.approved ? 'approved' : ''}"><div class="pair">
   <div><b>Escena ${s.idx + 1}</b> <span class="tag">original</span><img class="thumb" src="${file(s.frame)}"></div>
   <div>${s.img_state === 'running' ? '<span class="tag run">⏳ generando…</span> ' : ''}${s.img_state === 'error' ? `<div class="tag err" style="white-space:normal">❌ ${esc(s.img_error)}</div> ` : ''}${im ? `<span class="tag ${im.approved ? 'ok' : ''}">${im.approved ? '✓ aprobada' : 'generada'}</span>${im.qa ? (im.qa.ok ? ' <span class="tag ok">✓ revisada: avatar + acción</span>' : ' <span class="tag warn" title="' + esc(im.qa.differences) + '">⚠ ' + esc(im.qa.differences) + '</span>') : ''}${im.provider && im.provider !== S.p.settings.image_provider ? ' <span class="tag warn" title="' + esc((im.errors || []).join(' | ')) + '">respaldo: ' + esc(im.provider) + '</span>' : ''}${(im.errors || []).length ? '<details><summary>Por qué falló ' + esc(S.p.settings.image_provider) + '</summary><small>' + esc(im.errors.join(' | ')) + '</small></details>' : ''}<img class="thumb" src="${file(im.file)}">` : '<span class="tag">sin imagen</span><div class="thumb muted" style="display:grid;place-items:center">—</div>'}
     ${(s.versions || []).length > 1 ? `<div class="versions">${s.versions.map((v, n) => `<img class="${v.file === im?.file ? 'cur' : ''}" src="${file(v.file)}" data-act="version" data-i="${s.idx}" data-n="${n}" title="versión ${n + 1}">`).join('')}</div>` : ''}</div>
   <div><small class="muted">${esc(s.dialogue_en) || 'sin diálogo'}</small>
     <label>🎬 Acción que debe hacer TU avatar (editable; luego pulsa "Regenerar desde cero")</label><textarea data-scene="${s.idx}" data-field="action" style="min-height:70px">${esc(s.action || s.read?.person)}</textarea>
     <label>Cambios extra (ej. "que sonría", "quita la bandera")</label><textarea data-notes="${s.idx}" placeholder="Describe qué cambiar…"></textarea>
     <p><button ${running(jn) || !im ? 'disabled' : ''} data-act="regenEdit" data-i="${s.idx}">✏️ Retocar esta imagen</button> <button class="ghost" ${running(jn) ? 'disabled' : ''} data-act="regenNew" data-i="${s.idx}">🔄 Regenerar desde cero</button>
     <button class="${im?.approved ? 'ghost' : ''}" ${!im ? 'disabled' : ''} data-act="approve" data-i="${s.idx}">${im?.approved ? 'Quitar aprobación' : '✓ Aprobar'}</button></p>
     ${jobBox(jn, `Escena ${s.idx + 1}`)}</div></div></div>`; }).join('')}`;
}

function v4(p) {
  const sc = p.scenes, ok = sc.length && sc.every(s => s.image?.approved);
  if (!ok) return `<h2>Fase 4 · Guion por escena y prompts de video</h2><p class="muted">Aprueba todas las imágenes de la Fase 3 para continuar.</p>`;
  const has = sc.every(s => s.clips?.length);
  return `<h2>Fase 4 · Fragmentación del guion y prompts de video</h2><p class="muted">Cada imagen recibe su tramo exacto del guion (según las marcas de tiempo de cada palabra) y un prompt de video con el diálogo y la acción del avatar.</p>
  <p><button data-act="fragment" ${running('fragment') ? 'disabled' : ''}>✂️ ${has ? 'Rehacer fragmentación y prompts' : 'Fragmentar guion y crear prompts'}</button> ${has ? '<button data-goto="5">Continuar a la Fase 5 →</button>' : ''}</p>${jobBox('fragment', 'Fragmentación')}
  ${has ? sc.map(s => `<div class="card"><div class="row" style="flex-wrap:nowrap"><img class="thumb" style="width:110px" src="${file(s.image.file)}"><div class="grow"><b>Imagen ${s.idx + 1}</b> <span class="tag">${s.start.toFixed(1)}–${s.end.toFixed(1)}s original</span>
   ${s.clips.map(c => `<div class="card" style="background:var(--panel2);margin:8px 0"><b>Clip ${c.idx + 1}</b> <span class="tag">${c.t_start.toFixed(1)}s → ${c.t_end.toFixed(1)}s</span> <span class="tag">${c.target}s</span> ${c.action_es ? `<span class="muted">· ${esc(c.action_es)}</span>` : ''}
   ${c.dialogue_en && c.dialogue_en !== c.dialogue ? `<small class="muted">Original (EN): ${esc(c.dialogue_en)}</small>` : ''}<label>Diálogo del avatar (${c.lang === 'es' ? 'ES' : 'EN'}) — al editarlo se reconstruye el prompt</label><textarea data-clip="${s.idx}:${c.idx}" data-field="dialogue" style="min-height:44px">${esc(c.dialogue)}</textarea>
   <label>Prompt para la IA de video</label><textarea data-clip="${s.idx}:${c.idx}" data-field="video_prompt" style="min-height:90px">${esc(c.video_prompt)}</textarea></div>`).join('')}</div></div></div>`).join('') : ''}`;
}

function v5(p) {
  const sc = p.scenes, st = p.settings;
  if (!sc.length || !sc.every(s => s.clips?.length)) return `<h2>Fase 5 · Videos con IA</h2><p class="muted">Completa la Fase 4 primero.</p>`;
  const clips = sc.flatMap(s => s.clips.map(c => ({ s, c }))), done = clips.filter(x => x.c.status === 'done' && !x.c.stale).length;
  const vs = S.voices;
  return `<h2>Fase 5 · Generación de las escenas en video</h2><p class="muted">Veo 3.1 (vía DubVoice) anima cada imagen con su diálogo y acción. Después la voz de cada clip se unifica con la voz elegida en ElevenLabs para que todo el video suene igual.</p>
  <div class="card"><div class="row"><div class="grow"><label>Modelo DubVoice</label><select data-setting="dubvoice_video_model"><option value="veo-3.1-fast" ${st.dubvoice_video_model === 'veo-3.1-fast' ? 'selected' : ''}>veo-3.1-fast · 7.500 cr</option><option value="veo-3.1" ${st.dubvoice_video_model === 'veo-3.1' ? 'selected' : ''}>veo-3.1 · 17.000 cr</option><option value="veo-3.1-lite" ${st.dubvoice_video_model === 'veo-3.1-lite' ? 'selected' : ''}>veo-3.1-lite · 9.100 cr</option><option value="omniflash" ${st.dubvoice_video_model === 'omniflash' ? 'selected' : ''}>omniflash · duración por clip 4/6/8/10 s (4.688–9.375 cr)</option><option value="meta" ${st.dubvoice_video_model === 'meta' ? 'selected' : ''}>meta · 2.000 cr (sin verificar voz)</option></select></div>
  <div class="grow"><label class="inline"><input type="checkbox" data-setting="unify_voice" data-bool="1" ${st.unify_voice ? 'checked' : ''}> Unificar la voz con ElevenLabs (recomendado)</label>
  <div>Voz actual: <b>${esc(st.voice_name || 'sin elegir')}</b></div></div></div>
  ${st.unify_voice ? `<div style="max-width:320px"><label>Proveedor de voz</label><select data-setting="voice_provider"><option value="dubvoice" ${st.voice_provider === 'dubvoice' ? 'selected' : ''}>DubVoice.ai</option><option value="elevenlabs" ${st.voice_provider === 'elevenlabs' ? 'selected' : ''}>ElevenLabs</option></select></div><p><button class="ghost" data-act="loadVoices">🎙️ ${vs ? 'Recargar' : 'Cargar'} voces recomendadas para este avatar</button></p>
  ${vs ? vs.voices.slice(0, 12).map(v => `<div class="voice ${v.voice_id === st.voice_id ? 'sel' : ''}"><input type="radio" style="width:auto" name="voice" ${v.voice_id === st.voice_id ? 'checked' : ''} data-act="pickVoice" data-id="${esc(v.voice_id)}" data-name="${esc(v.name)}"><div class="grow"><b>${esc(v.name)}</b> <span class="tag">${esc(v.gender || '?')}</span> <span class="tag">${esc(v.age || '')}</span> <span class="tag">${esc(v.accent || '')}</span><br><small class="muted">${esc(v.descriptive)} ${esc(v.use_case)}</small></div>${v.preview_url ? `<audio controls preload="none" src="${esc(v.preview_url)}"></audio>` : ''}</div>`).join('') : ''}` : ''}</div>
  <div id="est" class="muted"></div>
  <div class="card" style="border-color:var(--acc)"><h3 style="margin-top:0">🧠 Supervisor Claude</h3><p class="muted">Un clic: lanza todos los clips, los vigila en vivo, audita cada resultado (audio, texto hablado, fotogramas) y decide solo si acepta, reintenta con otro prompt/modelo o cancela lo que se atasca. Protegido por límite de intentos y de créditos.</p>
  <p><button data-act="supervise" ${running('supervisor') || running('videos') ? 'disabled' : ''}>🚀 Generar y supervisar con Claude (1 clic)</button> ${running('supervisor') ? '<button class="danger" data-act="superStop">⏹ Detener supervisor</button>' : ''}</p>
  ${(job('supervisor')?.log || []).length ? `<div class="scroll" style="max-height:260px;background:#0c110e;border-radius:8px;padding:8px;font-size:12px">${job('supervisor').log.slice().reverse().map(l => `<div><span class="muted">${esc(l.t)}</span> ${esc(l.msg)}</div>`).join('')}</div>` : ''}
  ${jobBox('supervisor', 'Supervisor')}</div>
  <p><button class="ghost" data-act="genVideos" ${running('videos') ? 'disabled' : ''}>Generar clips pendientes (manual, sin supervisor)</button> <b>${done}/${clips.length} listos</b> ${done === clips.length ? '<button data-goto="6">Continuar a la Fase 6 →</button>' : ''}</p>
  ${jobBox('videos', 'Generación de videos')}
  <div class="grid">${clips.map(({ s, c }) => { const jn = `clip:${s.idx}:${c.idx}`; return `<div class="card"><b>Escena ${s.idx + 1} · clip ${c.idx + 1}</b> ${c.verified ? '<span class="tag ok">✓ auditado</span>' : c.status === 'done' && !c.stale ? '<span class="tag ok">listo</span>' : c.stale && c.status === 'done' ? '<span class="tag warn">desactualizado</span>' : c.status === 'running' ? '<span class="tag run">generando…</span>' : c.status === 'error' ? '<span class="tag err">error</span>' : '<span class="tag">pendiente</span>'}
   ${c.file ? `<video controls preload="metadata" class="thumb" src="${file(c.file)}?d=${Math.round(c.duration * 100)}"></video>` : `<img class="thumb" src="${file(s.image.file)}" style="opacity:.5">`}
   <small class="muted">${esc(c.dialogue) || '(sin diálogo)'}</small><div class="muted" style="font-size:11px">⏱ necesita ~${(c.target || 0).toFixed(1)} s${c.asked_seconds ? ` · pedido ${c.asked_seconds} s` : ''}</div>${c.warning ? `<div class="tag warn" style="white-space:normal">${esc(c.warning)}</div>` : ''}${c.error ? `<div class="tag err" style="white-space:normal">${esc(c.error)}</div>` : ''}
   <p><button class="ghost" ${running(jn) || running('videos') ? 'disabled' : ''} data-act="regenClip" data-i="${s.idx}" data-j="${c.idx}">🔄 ${c.file ? 'Regenerar' : 'Generar'}</button></p>${jobBox(jn, '')}${running(jn) || c.status === 'running' ? `<p><button class="danger" data-act="resetJob" data-job="${running(jn) ? jn : 'videos'}">⛔ Detener espera</button></p>` : ''}</div>`; }).join('')}</div>`;
}

function v6(p) {
  const st = p.settings, f = p.final;
  const ok = p.scenes.length && p.scenes.every(s => s.clips?.length && s.clips.every(c => c.status === 'done'));
  if (!ok) return `<h2>Fase 6 · Edición final</h2><p class="muted">Genera todos los clips de la Fase 5 primero.</p>`;
  return `<h2>Fase 6 · Edición y exportación</h2><p class="muted">Une los clips en orden, corta los silencios, agrega subtítulos Poppins (blanco con trazo negro; palabras clave en amarillo) y masteriza el audio.</p>
  <div class="card"><div class="row"><div class="grow"><label>Tamaño de letra</label><input type="number" data-setting="sub_font_size" data-num="1" value="${st.sub_font_size}"></div>
  <div class="grow"><label>Palabras por subtítulo</label><input type="number" min="1" max="6" data-setting="sub_words_per_chunk" data-num="1" value="${st.sub_words_per_chunk}"></div>
  <div class="grow"><label>Margen inferior (px)</label><input type="number" data-setting="sub_margin_v" data-num="1" value="${st.sub_margin_v}"></div>
  <div class="grow"><label class="inline"><input type="checkbox" data-setting="sub_uppercase" data-bool="1" ${st.sub_uppercase ? 'checked' : ''}> MAYÚSCULAS</label></div></div>
  <p><button data-act="edit" ${running('edit') ? 'disabled' : ''}>🎞️ ${f ? 'Volver a editar' : 'Editar y exportar'}</button></p>${jobBox('edit', 'Edición')}</div>
  ${f ? `<div class="card row"><div style="width:280px"><video controls class="thumb" src="${file(f.file)}?t=${Math.round(f.duration * 100)}"></video></div><div class="grow"><h3>Video final</h3><p>Duración: <b>${f.duration.toFixed(1)}s</b> · silencios eliminados: <b>${f.silence_removed}s</b> · coincidencia con el guion: <b>${Math.round(f.script_match * 100)}%</b></p>
  ${f.warning ? `<p class="tag warn" style="white-space:normal">${esc(f.warning)}</p>` : ''}<p>Palabras clave en amarillo: ${f.keywords.map(k => `<span class="tag">${esc(k)}</span>`).join(' ')}</p>
  <a href="${file(f.file)}" download="ESTRATEGIA_REMEDIOS_NATURALES.mp4"><button>⬇ Descargar MP4 1080×1920</button></a> <a href="${file(f.subs)}" download="subtitulos.ass"><button class="ghost">Subtítulos .ass</button></a></div></div>` : ''}`;
}

/* ------------------------------------------------------------- datos */
async function load(force) {
  if (!S.pid) return;
  try {
    const p = await api('GET', `/api/projects/${S.pid}`);
    const sig = JSON.stringify(p);
    if (sig === S.sig && !force) return;
    S.sig = sig; S.p = p;
    const ae = document.activeElement;
    if (!force && ae && /INPUT|TEXTAREA|SELECT/.test(ae.tagName) && $('#main').contains(ae) && ae.type !== 'radio') return; // no interrumpir la escritura
    render();
  } catch (e) { if (/no encontrado/i.test(e.message)) { S.pid = null; localStorage.removeItem('pid'); init(); } }
}
async function init() {
  const st = await api('GET', '/api/status').catch(() => null);
  if (st) {
    const names = { anthropic: 'Claude', elevenlabs: 'ElevenLabs', kie: 'Kie.ai', google: 'Google AI', pexels: 'Pexels', dubvoice: 'DubVoice' };
    $('#keys').innerHTML = Object.entries(st.keys).map(([k, v]) => `<span class="chip ${v.ok ? 'ok' : 'no'}" title="${v.ok ? v.var + ' ' + v.hint : 'no encontrada en ~/.zshrc'}">${v.ok ? '●' : '○'} ${names[k]}</span>`).join('') + `<span class="chip ${st.ffmpeg ? 'ok' : 'no'}">${st.ffmpeg ? '●' : '○'} ffmpeg</span>`;
  }
  const list = await api('GET', '/api/projects');
  if (!list.length) { const p = await api('POST', '/api/projects', { name: 'Mi primer video' }); list.push(p); }
  if (!list.find(x => x.id === S.pid)) S.pid = list[0].id;
  localStorage.pid = S.pid;
  $('#projSel').innerHTML = list.map(x => `<option value="${x.id}" ${x.id === S.pid ? 'selected' : ''}>${esc(x.name)}</option>`).join('');
  await load(true);
}

/* ------------------------------------------------------------- eventos */
document.addEventListener('click', async e => {
  const g = e.target.closest('[data-goto]'); if (g) { S.tab = +g.dataset.goto; localStorage.tab = S.tab; return render(); }
  const t = e.target.closest('[data-tab]'); if (t) { S.tab = +t.dataset.tab; localStorage.tab = S.tab; return render(); }
  const el = e.target.closest('[data-act]'); if (!el) return;
  const a = el.dataset.act, base = `/api/projects/${S.pid}`, i = el.dataset.i, j = el.dataset.j;
  try {
    if (a === 'uploadAvatar') { const f = $('#avatarFile').files[0]; if (!f) return toast('Elige una imagen', true); const fd = new FormData(); fd.append('file', f); await api('POST', `${base}/avatar`, fd, true); }
    else if (a === 'reAvatar') await api('POST', `${base}/avatar/analyze`);
    else if (a === 'uploadVideo') { const f = $('#videoFile').files[0]; if (!f) return toast('Elige un video', true); toast('Subiendo video…'); const fd = new FormData(); fd.append('file', f); await api('POST', `${base}/video`, fd, true); }
    else if (a === 'analyze') await api('POST', `${base}/analyze`, {});
    else if (a === 'reanalyze') { if (confirm('Se rehará todo el análisis (transcripción, traducción, escenas). ¿Continuar?')) await api('POST', `${base}/analyze`, { restart: true }); }
    else if (a === 'redoPrompts') { toast('Reescribiendo prompts de imagen…'); await api('POST', `${base}/analyze`, { force: ['prompts'] }); }
    else if (a === 'redoScenes') await api('POST', `${base}/analyze`, { force: ['scenes'] });
    else if (a === 'genImages') await api('POST', `${base}/images/generate`, {});
    else if (a === 'resetJob') { await api('POST', `${base}/jobs/${el.dataset.job}/reset`); toast('Tarea detenida. Puedes volver a lanzarla con su botón.'); }
    else if (a === 'regenAll') { if (confirm('Se generarán de nuevo todas las imágenes con tu avatar (las anteriores quedan en el historial de versiones y se gastan créditos otra vez). ¿Continuar?')) await api('POST', `${base}/images/generate`, { scenes: S.p.scenes.map(x => x.idx) }); }
    else if (a === 'approveAll') await api('POST', `${base}/images/approve_all`);
    else if (a === 'approve') { const s = S.p.scenes[i]; await api('POST', `${base}/images/${i}/approve`, { approved: !s.image.approved }); }
    else if (a === 'version') await api('POST', `${base}/images/${i}/version/${el.dataset.n}`);
    else if (a === 'regenEdit' || a === 'regenNew') {
      const notes = $(`[data-notes="${i}"]`).value.trim();
      if (a === 'regenEdit' && !notes) return toast('Escribe el cambio que quieres en la imagen', true);
      await api('POST', `${base}/images/${i}/regenerate`, { mode: a === 'regenEdit' ? 'edit' : 'new', notes });
    }
    else if (a === 'fragment') await api('POST', `${base}/fragment`);
    else if (a === 'loadVoices') { toast('Cargando voces…'); S.voices = await api('GET', `${base}/voices`); render(); return; }
    else if (a === 'pickVoice') { await api('PATCH', `${base}/settings`, { voice_id: el.dataset.id, voice_name: el.dataset.name }); }
    else if (a === 'supervise') await api('POST', `${base}/supervisor/start`);
    else if (a === 'superStop') { await api('POST', `${base}/supervisor/stop`); toast('Deteniendo el supervisor…'); }
    else if (a === 'genVideos') await api('POST', `${base}/videos/generate`, {});
    else if (a === 'regenClip') await api('POST', `${base}/videos/${i}/${j}/regenerate`);
    else if (a === 'edit') await api('POST', `${base}/edit`);
    else if (a === 'delProj') { if (confirm('¿Borrar este proyecto y todos sus archivos?')) { await api('DELETE', base); S.pid = null; return init(); } return; }
    await load(true);
  } catch (err) { load(true); }
});

document.addEventListener('change', async e => {
  const el = e.target, base = `/api/projects/${S.pid}`;
  try {
    if (el.id === 'projSel') { S.pid = el.value; localStorage.pid = S.pid; S.sig = ''; return load(true); }
    if (el.dataset.setting) {
      let v = el.value; if (el.dataset.bool) v = el.checked; else if (el.dataset.num) v = parseFloat(v);
      const body = {}; if (el.dataset.setting === 'name') body.name = v; else body[el.dataset.setting] = v;
      await api('PATCH', `${base}/settings`, body); await load(true);
    } else if (el.dataset.scene !== undefined) await api('PATCH', `${base}/scenes/${el.dataset.scene}`, { [el.dataset.field]: el.value });
    else if (el.dataset.clip) { const [i, j] = el.dataset.clip.split(':'); await api('PATCH', `${base}/scenes/${i}/clips/${j}`, { [el.dataset.field]: el.value }); await load(true); }
    else if (el.dataset.profile) await api('PATCH', `${base}/avatar/profile`, { [el.dataset.profile]: el.value });
    else if (el.dataset.profileVoice) await api('PATCH', `${base}/avatar/profile`, { voice: { [el.dataset.profileVoice]: el.value } });
  } catch (err) { /* toast ya mostrado */ }
});
$('#newProj').onclick = async () => { const n = prompt('Nombre del proyecto', 'Nuevo video'); if (!n) return; const p = await api('POST', '/api/projects', { name: n }); S.pid = p.id; S.tab = 1; init(); };
setInterval(() => load(false), 2000);
init();

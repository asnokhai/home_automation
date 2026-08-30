// The editing surface: timeline, cues, playback, and live bulb preview.
// The server holds the file; this holds the session and PUTs after every change.

const $ = (id) => document.getElementById(id);
let show = null, state = null, pps = 80, dirty = false;
let selected = new Set();   // note ids, so a selection survives edits
let clipboard = [];
let idSeq = 1;
const nextId = () => idSeq++;
let audio = new Audio("/audio");
audio.preload = "auto";

const WAVE_HEIGHT = 110;   // taller than the old 64px ribbon
const BASE = 0, TOP = 1;   // base carries the washes, top interrupts them
const DEFAULT = { level: 85, fade: 0, fadeOut: 0, dur: 1.0, color: ["hs", 210, 90] };

// ── colour ────────────────────────────────────────────────────────
function css(color) {
  if (!color) return "#555";
  if (color[0] === "ct") {                       // kelvin -> rough visual white
    const t = (color[1] - 2500) / 4000;
    return `rgb(255, ${Math.round(170 + 60 * t)}, ${Math.round(90 + 150 * t)})`;
  }
  return `hsl(${color[1]} ${color[2]}% 55%)`;
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(2).padStart(5, "0")}`;

// ── loading ───────────────────────────────────────────────────────
async function boot() {
  show = await (await fetch("/api/show")).json();
  state = await (await fetch("/api/state")).json();
  if (!show.cues) show.cues = [];
  if (!show.presets) show.presets = [];
  $("bpm").value = show.bpm || "";
  $("beatOffset").value = show.beatOffset || 0;
  show.cues.forEach((n) => { if (n.id == null) n.id = nextId(); });
  idSeq = Math.max(idSeq, ...show.cues.map((n) => n.id + 1), 1);
  renderPalette();
  $("previewState").textContent = state.preview ? "" : "(unavailable)";
  $("preview").disabled = !state.preview;

  // Build the lanes now, whatever the audio is doing. They used to be built
  // only from loadedmetadata, which fires while boot() is still awaiting its
  // fetches — so if the audio loaded first the event was missed and the
  // timeline came up empty while the playhead happily ran.
  build();

  const onMeta = () => {
    show.duration = audio.duration || show.duration;
    $("total").textContent = fmt(audio.duration);
    build();
    loadWave();
  };
  if (audio.readyState >= 1) onMeta();          // already loaded: no event coming
  else audio.addEventListener("loadedmetadata", onMeta);

  audio.addEventListener("error", () => {
    $("total").textContent = "no audio";
    show.duration = show.duration || 300;
    build();
  });
  tick();
}

// ── waveform ──────────────────────────────────────────────────────
// Decoded once by the browser, then painted a window at a time. Painting the
// whole song at high zoom would need a canvas wider than the browser allows
// (Chrome stops at ~65k pixels) and simply comes out blank.
let waveData = null;

async function loadWave() {
  try {
    const buf = await (await fetch("/audio")).arrayBuffer();
    const ctxA = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await ctxA.decodeAudioData(buf);
    waveData = { samples: decoded.getChannelData(0), rate: decoded.sampleRate };
    // the loudest point in the whole track, as a floor for the window scaling
    let peak = 0;
    const step = Math.max(1, Math.floor(waveData.samples.length / 200000));
    for (let i = 0; i < waveData.samples.length; i += step)
      peak = Math.max(peak, Math.abs(waveData.samples[i]));
    waveData.peak = peak || 1;
  } catch (e) {
    waveData = null;
  }
  paintWave();
  if (waveData && !show.bpm) detectBeat();
}

function paintWave() {
  const canvas = $("wave");
  const stage = $("stage");
  const width = Math.max(1, Math.min(stage.clientWidth || 1200, 8000));
  const from = stage.scrollLeft;

  canvas.style.left = from + "px";
  canvas.width = width;
  canvas.height = WAVE_HEIGHT;
  const g = canvas.getContext("2d");
  g.fillStyle = "#12141a";
  g.fillRect(0, 0, width, WAVE_HEIGHT);
  if (!waveData) return;

  const { samples, rate, peak } = waveData;
  const start = Math.max(0, Math.floor((from / pps) * rate));
  const per = Math.max(1, Math.floor(rate / pps));

  const peaks = new Float32Array(width);
  let loudest = 0;
  for (let x = 0; x < width; x++) {
    let hi = 0;
    const a = start + x * per, b = Math.min(a + per, samples.length);
    for (let i = a; i < b; i++) hi = Math.max(hi, Math.abs(samples[i]));
    peaks[x] = hi;
    loudest = Math.max(loudest, hi);
  }

  // Scale to the loudest thing *on screen*, so a quiet passage still shows its
  // beats instead of flatlining. Floored against the track peak so silence is
  // not amplified into a fake waveform.
  const ref = Math.max(loudest, peak * 0.08);
  g.fillStyle = "#4a6b91";
  for (let x = 0; x < width; x++) {
    const h = Math.min(1, peaks[x] / ref) * WAVE_HEIGHT;
    g.fillRect(x, (WAVE_HEIGHT - h) / 2, 1, Math.max(1, h));
  }
}

let wavePending = false;
function repaintWave() {
  if (wavePending) return;
  wavePending = true;
  requestAnimationFrame(() => { wavePending = false; paintWave(); });
}

// ── the beat grid ─────────────────────────────────────────────────
// Detected from the decoded audio the waveform already holds: an onset envelope
// (rising energy between short frames), autocorrelated to find the period, then
// swept for the phase that lands most onset energy on the grid. Both numbers are
// editable afterwards, because detection reliably finds the tempo and sometimes
// finds it at half or double speed.

function detectBeat() {
  if (!waveData) return flash("no audio to analyse");
  const { samples, rate } = waveData;
  const hop = 512, win = 1024;
  const frames = Math.floor((samples.length - win) / hop);
  if (frames < 64) return flash("too short to analyse");

  const env = new Float32Array(frames);
  let previous = 0;
  for (let f = 0; f < frames; f++) {
    let sum = 0;
    const a = f * hop;
    for (let i = a; i < a + win; i += 2) sum += samples[i] * samples[i];
    const energy = Math.sqrt(sum);
    env[f] = Math.max(0, energy - previous);      // rising edges only
    previous = energy;
  }

  const fps = rate / hop;
  let best = -1, bpm = 120;
  for (let candidate = 70; candidate <= 180; candidate += 0.25) {
    const lag = Math.round((60 / candidate) * fps);
    if (lag < 2 || lag >= frames) continue;
    let score = 0;
    for (let i = 0; i + lag < frames; i++) score += env[i] * env[i + lag];
    score /= frames - lag;                        // do not favour short lags
    if (score > best) { best = score; bpm = candidate; }
  }

  const period = (60 / bpm) * fps;
  let bestPhase = 0, bestScore = -1;
  for (let phase = 0; phase < period; phase += 0.25) {
    let score = 0;
    for (let k = 0; ; k++) {
      const idx = Math.round(phase + k * period);
      if (idx >= frames) break;
      score += env[idx];
    }
    if (score > bestScore) { bestScore = score; bestPhase = phase; }
  }

  show.bpm = +bpm.toFixed(2);
  show.beatOffset = +(bestPhase * hop / rate).toFixed(3);
  $("bpm").value = show.bpm;
  $("beatOffset").value = show.beatOffset;
  paintGrid();
  save();
  flash(`${show.bpm} bpm, first beat ${show.beatOffset.toFixed(2)}s`);
}

// The grid is regular, so CSS gradients draw it for free — no elements, and it
// scrolls and zooms with the lanes.
function paintGrid() {
  const lanes = $("lanes");
  if (!show.bpm) { lanes.style.backgroundImage = "none"; return; }
  const beat = (60 / show.bpm) * pps;
  const offset = (show.beatOffset || 0) * pps;
  lanes.style.backgroundImage =
    `repeating-linear-gradient(90deg, #ffffff26 0 1px, transparent 1px ${4 * beat}px),` +
    `repeating-linear-gradient(90deg, #ffffff10 0 1px, transparent 1px ${beat}px)`;
  lanes.style.backgroundPosition = `${offset}px 0, ${offset}px 0`;
}

// Holding shift bypasses this, as does setting the selector to "off".
function snap(t, e) {
  if ((e && e.shiftKey) || !show.bpm) return t;
  const division = +$("snap").value;
  if (!division) return t;
  const period = (60 / show.bpm) * division;
  const offset = show.beatOffset || 0;
  return Math.max(0, +(Math.round((t - offset) / period) * period + offset).toFixed(3));
}

// ── building the lanes ────────────────────────────────────────────
function build() {
  const width = Math.max(1, Math.round((show.duration || audio.duration || 300) * pps));
  $("waveBox").style.width = width + "px";
  $("ruler").style.width = width + "px";
  $("lanes").style.width = width + "px";

  const step = pps < 40 ? 30 : pps < 120 ? 10 : 5;
  $("ruler").innerHTML = "";
  for (let t = 0; t < (show.duration || 300); t += step) {
    const tag = document.createElement("span");
    tag.style.left = (t * pps) + "px";
    tag.textContent = fmt(t).replace(/\.\d+$/, "");
    $("ruler").appendChild(tag);
  }

  $("lanes").innerHTML = "";
  show.lights.forEach((light) => {
    const group = document.createElement("div");
    group.className = "group";
    group.style.width = width + "px";
    [TOP, BASE].forEach((layer) => {          // top row above the base row
      const lane = document.createElement("div");
      lane.className = "lane " + (layer === TOP ? "top" : "base");
      lane.dataset.light = light;
      lane.dataset.layer = layer;
      lane.style.width = width + "px";
      lane.innerHTML = `<span class="name">${layer === TOP ? light : ""}`
                     + `<i>${layer === TOP ? "top" : "base"}</i></span>`;
      lane.addEventListener("mousedown", (e) => onLaneDown(e, lane));
      group.appendChild(lane);
    });
    $("lanes").appendChild(group);
  });
  render();
  paintGrid();
}

function render() {
  document.querySelectorAll(".cue").forEach((n) => n.remove());
  show.cues.forEach((note) => {
    const lane = document.querySelector(
      `.lane[data-light="${CSS.escape(note.light)}"][data-layer="${note.layer ?? BASE}"]`);
    if (!lane) return;
    const dur = note.dur ?? DEFAULT.dur;

    const el = document.createElement("div");
    el.className = "cue" + (selected.has(note.id) ? " sel" : "");
    el.style.left = (note.t * pps) + "px";
    el.style.width = Math.max(6, dur * pps) + "px";
    el.style.background = css(note.color);
    el.style.opacity = 0.35 + 0.65 * (note.level / 100);
    el.dataset.id = note.id;
    el.title = `${note.light} · ${note.t.toFixed(2)}s for ${dur.toFixed(2)}s · `
             + `${note.level}%`;

    if (note.fade > 0) {
      const ramp = document.createElement("div");
      ramp.className = "ramp";
      ramp.style.width = Math.min(100, (note.fade / dur) * 100) + "%";
      el.appendChild(ramp);
    }
    if (note.fadeOut > 0) {
      const out = document.createElement("div");
      out.className = "ramp-out";
      out.style.width = Math.min(100, (note.fadeOut / dur) * 100) + "%";
      el.appendChild(out);
    }
    const grip = document.createElement("div");
    grip.className = "grip";
    el.appendChild(grip);
    lane.appendChild(el);
  });
  inspector();
}

// ── selection ─────────────────────────────────────────────────────
const byId = (id) => show.cues.find((n) => n.id === id);
const chosen = () => show.cues.filter((n) => selected.has(n.id));

function selectOnly(id) {
  selected.clear();
  if (id != null) selected.add(id);
  render();
}

// A drag across empty lane space is a rubber band; a click that never moves is
// a new note. Telling them apart by distance keeps both on the same button.
function rubberBand(e, lane) {
  const box = document.createElement("div");
  box.id = "band";
  const laneLeft = lane.getBoundingClientRect().left;
  const x0 = e.clientX, y0 = e.clientY;
  let moved = false;

  const move = (ev) => {
    if (!moved && Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) < 4) return;
    if (!moved) { moved = true; document.body.appendChild(box); }
    const l = Math.min(x0, ev.clientX), r = Math.max(x0, ev.clientX);
    const t = Math.min(y0, ev.clientY), b = Math.max(y0, ev.clientY);
    Object.assign(box.style, { left: l + "px", top: t + "px",
                               width: (r - l) + "px", height: (b - t) + "px" });
    if (!ev.shiftKey) selected.clear();
    document.querySelectorAll(".cue").forEach((el) => {
      const q = el.getBoundingClientRect();
      if (q.right >= l && q.left <= r && q.bottom >= t && q.top <= b)
        selected.add(+el.dataset.id);
    });
    render();
  };
  const up = (ev) => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    box.remove();
    if (!moved) place(lane, snap(clamp((ev.clientX - laneLeft) / pps,
                                       0, show.duration || 1e9), ev));
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
  e.preventDefault();
}

function onLaneDown(e, lane) {
  const hit = e.target.closest(".cue");
  if (!hit) return rubberBand(e, lane);

  const id = +hit.dataset.id;
  if (e.shiftKey || e.ctrlKey || e.metaKey) {     // add or remove from the group
    selected.has(id) ? selected.delete(id) : selected.add(id);
    render();
    return;
  }
  if (!selected.has(id)) selectOnly(id);

  const note = byId(id);
  const resizing = e.target.classList.contains("grip");
  const left = () => lane.getBoundingClientRect().left;
  const grab = (e.clientX - left()) - note.t * pps;
  // dragging one of a selection drags them all, keeping their spacing
  const group = chosen().map((n) => ({ n, dt: n.t - note.t }));

  const move = (ev) => {
    const px = ev.clientX - left();
    if (resizing) {
      const edge = snap(px / pps, ev);
      const dur = Math.max(0.05, +(edge - note.t).toFixed(3));
      chosen().forEach((n) => { n.dur = dur; });
    } else {
      const head = Math.max(0, snap((px - grab) / pps, ev));
      if (group.every(({ dt }) => head + dt >= 0))
        group.forEach(({ n, dt }) => { n.t = +(head + dt).toFixed(3); });
    }
    render();
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    save();
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
  e.preventDefault();
}

function place(lane, t) {
  const light = lane.dataset.light, layer = +lane.dataset.layer;
  const last = show.cues.filter((c) => c.light === light &&
                                (c.layer ?? BASE) === layer && c.t <= t)
                        .sort((a, b) => a.t - b.t).pop();
  const note = {
    id: nextId(), t: +t.toFixed(3), dur: DEFAULT.dur, light, layer,
    level: last ? last.level : DEFAULT.level,
    color: last && last.color ? last.color.slice() : DEFAULT.color.slice(),
    fade: DEFAULT.fade,
  };
  show.cues.push(note);
  selectOnly(note.id);
  save();
  previewAt(audio.currentTime);
}

function remove() {
  if (!selected.size) return;
  show.cues = show.cues.filter((n) => !selected.has(n.id));
  selected.clear();
  render();
  save();
}

// ── copy and paste ────────────────────────────────────────────────
function copy() {
  const notes = chosen();
  if (!notes.length) return;
  const head = Math.min(...notes.map((n) => n.t));
  // held relative to the first note, so pasting keeps the group's shape
  clipboard = notes.map((n) => ({ ...n, dt: +(n.t - head).toFixed(3) }));
  flash(`copied ${notes.length} note${notes.length > 1 ? "s" : ""}`);
}

function paste(at = audio.currentTime) {
  if (!clipboard.length) return;
  selected.clear();
  clipboard.forEach((c) => {
    const note = { ...c, id: nextId(), t: +(at + c.dt).toFixed(3) };
    delete note.dt;
    note.color = c.color ? c.color.slice() : null;
    show.cues.push(note);
    selected.add(note.id);
  });
  render();
  save();
  flash(`pasted ${clipboard.length} at ${fmt(at)}`);
}

// ── the inspector ─────────────────────────────────────────────────
function inspector() {
  const box = $("inspector");
  const notes = chosen();
  if (!notes.length) { box.hidden = true; return; }
  const cue = notes[0];
  box.hidden = false;
  if (!cue.color) cue.color = DEFAULT.color.slice();

  $("selTitle").textContent = notes.length > 1
    ? `${notes.length} notes selected`
    : `${cue.light} @ ${cue.t.toFixed(2)}s`;
  $("selLevel").value = cue.level;  $("selLevelOut").textContent = cue.level + "%";
  $("selDur").value = (cue.dur ?? DEFAULT.dur).toFixed(2);
  $("selFade").value = cue.fade || 0;
  $("selFadeOut").value = cue.fadeOut || 0;
  const mode = cue.color[0];
  $("selMode").value = mode;
  $("hsFields").hidden = mode !== "hs";
  $("ctFields").hidden = mode !== "ct";
  if (mode === "hs") {
    $("selHue").value = cue.color[1]; $("selHueOut").textContent = cue.color[1] + "°";
    $("selSat").value = cue.color[2]; $("selSatOut").textContent = cue.color[2] + "%";
  } else {
    $("selK").value = cue.color[1]; $("selKOut").textContent = cue.color[1] + "K";
  }
  $("swatch").style.background = css(cue.color);
}

// an edit lands on every selected note, so a whole group recolours at once
function edit(change) {
  const notes = chosen();
  if (!notes.length) return;
  notes.forEach((n) => { if (!n.color) n.color = DEFAULT.color.slice(); change(n); });
  render();
  save();
  previewAt(audio.currentTime);
}

// ── saving and preview ────────────────────────────────────────────
function flash(message) {
  $("status").textContent = message;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { if (!dirty) $("status").textContent = "saved"; }, 1600);
}

let saveTimer = null;
function save() {
  dirty = true;
  $("status").textContent = "saving…";
  $("status").className = "dirty";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const res = await fetch("/api/show", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cues: show.cues, duration: show.duration,
                             offset: show.offset || 0,
                             presets: show.presets || [],
                             bpm: show.bpm || 0,
                             beatOffset: show.beatOffset || 0 }),
    });
    const info = await res.json();
    dirty = false;
    $("status").textContent = `saved · ${info.cues} cues`;
    $("status").className = "";
  }, 250);
}

let lastPreview = {};
function preview(cue) {
  if (!$("preview").checked || !cue) return;
  const key = `${cue.level}|${(cue.color || []).join(",")}`;
  if (lastPreview[cue.light] === key) return;
  lastPreview[cue.light] = key;
  fetch("/api/preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ light: cue.light, level: cue.level, color: cue.color }),
  }).catch(() => {});
}

// The note covering this instant, if any. Later notes win where they overlap,
// matching how the compiler resolves it.
function noteAt(light, t) {
  return show.cues
    .filter((n) => n.light === light && n.t <= t && t < n.t + (n.dur ?? DEFAULT.dur))
    .sort((a, b) => a.t - b.t)
    .pop();
}

// What every light should be showing at time t. Outside its notes a light is
// off — not left holding the last colour it was given.
function previewAt(t) {
  if (!$("preview").checked) return;
  show.lights.forEach((light) => {
    const note = noteAt(light, t);
    if (!note) {
      preview({ light, level: "off", color: null });
      return;
    }
    let level = note.level;
    const dur = note.dur ?? DEFAULT.dur;
    const into = t - note.t;                   // how far in
    const left = (note.t + dur) - t;           // how far from the end
    if (note.fade > 0 && into < note.fade)
      level = Math.round(note.level * (into / note.fade));
    if (note.fadeOut > 0 && left < note.fadeOut)
      level = Math.round(note.level * (left / note.fadeOut));
    preview({ light, level: Math.max(1, level), color: note.color });
  });
}

// ── saved looks ───────────────────────────────────────────────────
function renderPalette() {
  const bar = $("palette");
  bar.innerHTML = "";
  (show.presets || []).forEach((look, i) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.style.background = css(look.color);
    chip.style.opacity = 0.35 + 0.65 * (look.level / 100);
    chip.title = `${look.level}% · ${colorLabel(look.color)}
click to apply · alt-click to remove`;
    chip.textContent = look.level + "%";
    chip.addEventListener("click", (e) => {
      if (e.altKey) {
        show.presets.splice(i, 1);
        renderPalette();
        save();
        return;
      }
      applyLook(look);
    });
    bar.appendChild(chip);
  });
  if (!(show.presets || []).length) {
    bar.insertAdjacentHTML("beforeend",
      '<i class="hint">nothing saved yet — set a note up, then hit save</i>');
  }
}

function colorLabel(color) {
  if (!color) return "no colour";
  return color[0] === "ct" ? color[1] + "K" : `hue ${color[1]}°, sat ${color[2]}%`;
}

// Applying a look changes the selected note; with nothing selected it becomes
// what the next note you place will use.
function applyLook(look) {
  if (selected.size) {
    edit((c) => { c.level = look.level; c.color = look.color.slice(); });
  } else {
    DEFAULT.level = look.level;
    DEFAULT.color = look.color.slice();
    $("status").textContent = `new notes: ${look.level}% ${colorLabel(look.color)}`;
  }
}

function saveLook() {
  const from = chosen()[0] || DEFAULT;
  if (!show.presets) show.presets = [];
  $("bpm").value = show.bpm || "";
  $("beatOffset").value = show.beatOffset || 0;
  show.cues.forEach((n) => { if (n.id == null) n.id = nextId(); });
  idSeq = Math.max(idSeq, ...show.cues.map((n) => n.id + 1), 1);
  const look = { level: from.level, color: (from.color || DEFAULT.color).slice() };
  const key = (l) => `${l.level}|${l.color.join(",")}`;
  if (show.presets.some((l) => key(l) === key(look))) return;   // already there
  show.presets.push(look);
  renderPalette();
  save();
}

function allOff() {
  show.lights.forEach((light) => preview({ light, level: "off", color: null }));
}

// ── transport ─────────────────────────────────────────────────────
function toggle() {
  if (audio.paused) { audio.play().catch(() => {}); $("play").textContent = "⏸"; }
  else { audio.pause(); $("play").textContent = "▶"; }
}
function seek(to) {
  audio.currentTime = clamp(to, 0, audio.duration || show.duration || 0);
  previewAt(audio.currentTime + (audio.paused ? 0 : lead()));
}

let lastPreviewAt = -1;

// A bulb takes roughly 300ms to answer, so previewing "now" always arrives late.
// While the song is running, preview slightly ahead of the playhead and the
// change lands on the beat. Parked, there is nothing to run ahead of.
const lead = () => (+$("lead").value || 0) / 1000;

function tick() {
  const t = audio.currentTime || 0;
  $("clock").textContent = fmt(t);
  $("playhead").style.left = (t * pps) + "px";

  if (!audio.paused) {
    const stage = $("stage"), x = t * pps;          // keep the playhead in view
    if (x < stage.scrollLeft || x > stage.scrollLeft + stage.clientWidth - 80)
      stage.scrollLeft = x - stage.clientWidth * 0.3;
    if (t - lastPreviewAt > 0.04 || t < lastPreviewAt) {
      previewAt(t + lead());
      lastPreviewAt = t;
    }
  }
  requestAnimationFrame(tick);
}

// ── wiring ────────────────────────────────────────────────────────
$("play").addEventListener("click", toggle);

// the canvas is a window onto the song, so it has to follow the scroll
$("stage").addEventListener("scroll", repaintWave);
window.addEventListener("resize", repaintWave);

// Click anywhere on the waveform or the ruler to jump there, and hold to scrub.
// Measure against a full-width element in the scrolling content: the canvas is
// only a window onto the song, parked at the scroll offset, so its own left edge
// is the edge of the *view*, not of the timeline.
["waveBox", "ruler"].forEach((id) => {
  $(id).addEventListener("mousedown", (e) => {
    const origin = () => $("waveBox").getBoundingClientRect().left;
    const at = (ev) => seek((ev.clientX - origin()) / pps);
    at(e);
    const move = (ev) => at(ev);
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    e.preventDefault();
  });
});
$("zoom").addEventListener("input", (e) => {
  const at = audio.currentTime;
  pps = +e.target.value;
  build(); repaintWave(); paintGrid();
  $("stage").scrollLeft = at * pps - $("stage").clientWidth / 2;
  repaintWave();
});
$("preview").addEventListener("change", () => {
  lastPreview = {};
  if ($("preview").checked) previewAt(audio.currentTime + (audio.paused ? 0 : lead()));
  else allOff();                  // do not leave the room stuck on a note's colour
});

$("selLevel").addEventListener("input", (e) => edit((c) => c.level = +e.target.value));
$("selDur").addEventListener("input", (e) =>
  edit((c) => c.dur = Math.max(0.05, +e.target.value)));
$("selFade").addEventListener("input", (e) => edit((c) => c.fade = +e.target.value));
$("selFadeOut").addEventListener("input", (e) =>
  edit((c) => c.fadeOut = Math.max(0, +e.target.value)));
$("selMode").addEventListener("change", (e) => edit((c) =>
  c.color = e.target.value === "ct" ? ["ct", 3000] : ["hs", 210, 90]));
$("selHue").addEventListener("input", (e) => edit((c) =>
  c.color = ["hs", +e.target.value, (c.color && c.color[0] === "hs" ? c.color[2] : 90)]));
$("selSat").addEventListener("input", (e) => edit((c) =>
  c.color = ["hs", (c.color && c.color[0] === "hs" ? c.color[1] : 210), +e.target.value]));
$("selK").addEventListener("input", (e) => edit((c) => c.color = ["ct", +e.target.value]));
$("selDelete").addEventListener("click", remove);
$("saveLook").addEventListener("click", saveLook);
$("detect").addEventListener("click", detectBeat);
$("bpm").addEventListener("change", (e) => {
  show.bpm = +e.target.value || 0; paintGrid(); save();
});
$("beatOffset").addEventListener("change", (e) => {
  show.beatOffset = +e.target.value || 0; paintGrid(); save();
});
$("snap").addEventListener("change", () => {});

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input,select")) return;

  if (e.ctrlKey || e.metaKey) {                 // clipboard and selection
    const k = e.key.toLowerCase();
    if (k === "c") { e.preventDefault(); copy(); return; }
    if (k === "v") { e.preventDefault(); paste(); return; }
    if (k === "x") { e.preventDefault(); copy(); remove(); return; }
    if (k === "d") { e.preventDefault(); copy(); paste(); return; }
    if (k === "a") {
      e.preventDefault();
      selected = new Set(show.cues.map((n) => n.id));
      render();
      return;
    }
  }

  const jump = e.shiftKey ? 5 : 1;
  if (e.code === "Space") { e.preventDefault(); toggle(); }
  else if (e.code === "ArrowLeft") { e.preventDefault(); seek(audio.currentTime - jump); }
  else if (e.code === "ArrowRight") { e.preventDefault(); seek(audio.currentTime + jump); }
  else if (e.code === "Home") seek(0);
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); remove(); }
  else if (e.key === "Escape") { selected.clear(); render(); }
  else if (/^[1-8]$/.test(e.key)) {               // place on lane N at the playhead
    const lane = document.querySelectorAll(".lane")[+e.key - 1];
    if (lane) place(lane, snap(audio.currentTime, e));
  }
});

window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});

boot();

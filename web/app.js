// Pulso — front end. No framework, no build step.
// Sections: format · ECG monitor · vitals · prompts · markdown · SQL · tables · charts · agent stream.

const $ = (sel, el = document) => el.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
};
const SVGNS = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}) => {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  return el;
};
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";
const EASE_IN_OUT = "cubic-bezier(0.77, 0, 0.175, 1)";
const PALETTE = ["#1F3D74", "#C2800F", "#17150F", "#1E6B65", "#8C4B73", "#6F8FBF", "#7C8B4A", "#A39D8C"];

// ------------------------------------------------------------------ format
const nf0 = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 2 });
const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
const isDate = (v) => typeof v === "string" && /^\d{4}-\d{2}(-\d{2})?$/.test(v);
const monthLabel = (v, long = false) => {
  const [y, m] = v.split("-");
  return `${MONTHS[+m - 1]}${long ? " " + y : " " + y.slice(2)}`;
};
function fmtUSD(v, compact = false) {
  if (v == null) return "—";
  const a = Math.abs(v), sign = v < 0 ? "−" : "";
  if (compact) {
    if (a >= 1e6) return `${sign}$${nf2.format(a / 1e6)}M`;
    if (a >= 1e4) return `${sign}$${nf0.format(a / 1e3)}k`;
    if (a >= 1e3) return `${sign}$${nf1.format(a / 1e3)}k`;
  }
  return `${sign}$${(a >= 100 ? nf0 : usd2).format(a)}`;
}
const usd2 = new Intl.NumberFormat("es-MX", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// "cac_paid_social" -> "CAC paid social", "new_mrr_usd" -> "new MRR"
const ACRONYMS = new Set(["mrr", "arr", "nrr", "grr", "cac", "ltv", "arpa", "pp", "mx", "co", "pe", "cr", "do", "q1", "q2", "q3", "q4"]);
const humanize = (name) => String(name).replace(/_usd$|_pct$/i, "").split("_")
  .map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w)).join(" ");
const fmtPct = (v, digits = 1) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
function fmtNum(v, compact = false) {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (compact && a >= 1e6) return `${nf2.format(v / 1e6)}M`;
  if (compact && a >= 1e4) return `${nf0.format(v / 1e3)}k`;
  if (Number.isInteger(v)) return nf0.format(v);
  return a >= 100 ? nf0.format(v) : a >= 1 ? nf2.format(v) : v.toPrecision(3);
}
const formatter = (kind, compact) =>
  kind === "usd" ? (v) => fmtUSD(v, compact) : kind === "percent" ? (v) => fmtPct(v) : (v) => fmtNum(v, compact);
function columnKind(name, values) {
  const n = name.toLowerCase();
  if (/(_usd|usd_|^mrr|^arr|revenue|spend|cac|ltv|arpa|price|precio)/.test(n)) return "usd";
  if (/(rate|churn|nrr|grr|pct|share|ratio|tasa|retention|conversion)/.test(n)) {
    const nums = values.filter((v) => typeof v === "number");
    if (nums.length && nums.every((v) => Math.abs(v) <= 2)) return "percent";
  }
  return "number";
}
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ------------------------------------------------------------- ECG monitor
const ecg = (() => {
  const canvas = $("#ecg");
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, dpr = 1, ys = [], cs = [], x = 0, last = performance.now();
  let t = 0, nextBeat = 0.4, beatStart = -10, beatAmp = 0.6, beatColor = "#1F3D74";
  const mode = { bpm: 58, amp: 0.55, speed: 95, color: "#1F3D74" };
  const queued = [];
  const COLORS = { signal: "#1F3D74", amber: "#C2800F", teal: "#1E6B65", ink: "#17150F" };

  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(1, Math.floor(r.width)); H = Math.max(1, Math.floor(r.height));
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ys = new Array(W).fill(null); cs = new Array(W).fill(COLORS.signal);
    x = 0;
  }
  const g = (p, mu, sd, a) => a * Math.exp(-((p - mu) ** 2) / (2 * sd * sd));
  function waveform(sec) {
    const p = sec / 0.62; // a heartbeat lasts ~0.62 s regardless of rate
    if (p < 0 || p > 1) return 0;
    return g(p, 0.12, 0.03, 0.11) + g(p, 0.255, 0.012, -0.14) + g(p, 0.29, 0.014, 1) +
      g(p, 0.325, 0.014, -0.28) + g(p, 0.6, 0.065, 0.24);
  }
  function sample(dt) {
    t += dt;
    if (t >= nextBeat) {
      const q = queued.shift();
      beatStart = t;
      beatAmp = q?.amp ?? mode.amp * (0.94 + Math.random() * 0.1);
      beatColor = q?.color ?? mode.color;
      const interval = 60 / (q?.bpm ?? mode.bpm);
      nextBeat = t + interval * (q?.early ? 0.55 : 0.97 + Math.random() * 0.06);
    }
    const v = waveform(t - beatStart) * beatAmp + (Math.random() - 0.5) * 0.012;
    return [v, beatColor];
  }
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const advance = mode.speed * dt;
    const steps = Math.max(1, Math.round(advance));
    for (let i = 0; i < steps; i++) {
      const [v, c] = sample(dt / steps);
      x = (x + 1) % W;
      ys[x] = v; cs[x] = c;
      for (let k = 1; k <= 26; k++) ys[(x + k) % W] = null; // erase ahead of the sweep
    }
    draw();
    requestAnimationFrame(frame);
  }
  function draw() {
    ctx.clearRect(0, 0, W, H);
    const base = H * 0.64, scale = H * 0.5;
    ctx.lineWidth = 1.6; ctx.lineJoin = "round"; ctx.lineCap = "round";
    let started = false, color = null;
    for (let i = 0; i < W; i++) {
      const v = ys[i];
      if (v == null) { if (started) ctx.stroke(); started = false; continue; }
      const y = base - v * scale;
      if (!started || cs[i] !== color) {
        if (started) { ctx.lineTo(i, y); ctx.stroke(); }
        color = cs[i];
        ctx.strokeStyle = color;
        ctx.beginPath(); ctx.moveTo(i, y); started = true;
      } else ctx.lineTo(i, y);
    }
    if (started) ctx.stroke();
    // write head
    if (ys[x] != null) {
      const y = base - ys[x] * scale;
      ctx.fillStyle = cs[x];
      ctx.globalAlpha = 0.18; ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(x, y, 2.4, 0, Math.PI * 2); ctx.fill();
    }
  }
  function drawStatic() {
    resize();
    let tt = 0;
    for (let i = 0; i < W; i++) {
      tt += 1 / 95;
      const ph = tt % (60 / 58);
      ys[i] = waveform(ph - 0.1) * 0.55;
    }
    x = W - 1;
    draw();
  }

  new ResizeObserver(() => (reduceMotion ? drawStatic() : resize())).observe(canvas);
  if (reduceMotion) drawStatic(); else { resize(); requestAnimationFrame(frame); }

  return {
    set(state) {
      if (state === "working") Object.assign(mode, { bpm: 104, amp: 0.78, speed: 150, color: COLORS.signal });
      else if (state === "fixing") Object.assign(mode, { bpm: 112, amp: 0.7, speed: 150, color: COLORS.amber });
      else if (state === "done") Object.assign(mode, { bpm: 66, amp: 0.6, speed: 105, color: COLORS.teal });
      else Object.assign(mode, { bpm: 58, amp: 0.55, speed: 95, color: COLORS.signal });
    },
    beat(color = COLORS.signal, amp = 1) { queued.push({ amp, color, early: true, bpm: mode.bpm }); },
    glitch() {
      queued.push({ amp: 0.38, color: COLORS.amber, early: true, bpm: 150 });
      queued.push({ amp: 0.95, color: COLORS.amber, early: true, bpm: 140 });
    },
  };
})();

// ------------------------------------------------------------- app state
const state = { busy: false, entries: 0, overview: null, session: getSession(), t0: 0, clockRaf: 0 };
function getSession() {
  let id = null;
  try { id = sessionStorage.getItem("pulso-session"); } catch {}
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
    try { sessionStorage.setItem("pulso-session", id); } catch {}
  }
  return id;
}
const STATE_LABEL = { idle: "En reposo", working: "Analizando", fixing: "Autocorrigiendo", done: "Respuesta lista" };
let doneTimer = 0;
function setState(st) {
  clearTimeout(doneTimer);
  document.body.dataset.state = st;
  $("#state").textContent = STATE_LABEL[st];
  ecg.set(st);
  if (st === "working" && !state.clockRaf) startClock();
  if (st === "done" || st === "idle") stopClock();
  if (st === "done") doneTimer = setTimeout(() => !state.busy && setState("idle"), 4000);
}
const fmtClock = (ms) => {
  const t = ms / 1000, m = Math.floor(t / 60);
  return `${String(m).padStart(2, "0")}:${(t - m * 60).toFixed(1).padStart(4, "0")}`;
};
function startClock() {
  state.t0 = performance.now();
  const tick = () => {
    const txt = fmtClock(performance.now() - state.t0);
    $("#clock").textContent = txt;
    const live = $(".live time");
    if (live) live.textContent = txt;
    state.clockRaf = requestAnimationFrame(tick);
  };
  tick();
}
function stopClock() { cancelAnimationFrame(state.clockRaf); state.clockRaf = 0; }

// ------------------------------------------------------------------ vitals
function countUp(el, to, fmt, delay = 0) {
  if (reduceMotion || to == null) { el.textContent = fmt(to); return; }
  const dur = 1100, t0 = performance.now() + delay;
  const step = (now) => {
    const p = Math.min(1, Math.max(0, (now - t0) / dur));
    const e = 1 - Math.pow(1 - p, 4);
    el.textContent = fmt(to * e);
    if (p < 1) requestAnimationFrame(step);
  };
  el.textContent = fmt(0);
  requestAnimationFrame(step);
}
function sparkline(values, i) {
  const w = 112, hgt = 40, pad = 4;
  const pts = values.map((v, k) => [k, v]).filter(([, v]) => v != null);
  const svg = s("svg", { class: "spark", viewBox: `0 0 ${w} ${hgt}`, "aria-hidden": "true" });
  if (pts.length < 2) return svg;
  const min = Math.min(...pts.map((p) => p[1])), max = Math.max(...pts.map((p) => p[1]));
  const X = (k) => pad + (k / (values.length - 1)) * (w - pad * 2);
  const Y = (v) => hgt - pad - ((v - min) / (max - min || 1)) * (hgt - pad * 2);
  const d = pts.map(([k, v], j) => `${j ? "L" : "M"}${X(k).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const gid = `spark-fade-${i}`;
  const defs = s("defs");
  const lg = s("linearGradient", { id: gid, x1: 0, x2: 0, y1: 0, y2: 1 });
  lg.append(s("stop", { offset: "0%", "stop-color": "#17150F", "stop-opacity": 0.12 }), s("stop", { offset: "100%", "stop-color": "#17150F", "stop-opacity": 0 }));
  defs.append(lg);
  const [lk, lv] = pts[pts.length - 1];
  const fill = s("path", { class: "fill", d: `${d}L${X(lk)},${hgt}L${X(pts[0][0])},${hgt}Z`, fill: `url(#${gid})`, style: "fill:url(#" + gid + ")" });
  const line = s("path", { class: "line", d });
  const halo = s("circle", { class: "halo", cx: X(lk), cy: Y(lv), r: 5 });
  const dot = s("circle", { cx: X(lk), cy: Y(lv), r: 2.3 });
  svg.append(defs, fill, line, halo, dot);
  if (!reduceMotion) {
    requestAnimationFrame(() => {
      const len = line.getTotalLength();
      line.style.strokeDasharray = len;
      line.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }], { duration: 1300, delay: 200 + i * 90, easing: EASE_IN_OUT, fill: "both" });
      fill.animate([{ opacity: 0 }, { opacity: 0.9 }], { duration: 700, delay: 900 + i * 90, easing: "ease", fill: "both" });
      for (const c of [halo, dot]) c.animate([{ opacity: 0, transform: "scale(.6)" }, { opacity: 1, transform: "scale(1)" }],
        { duration: 400, delay: 1350 + i * 90, easing: EASE_OUT, fill: "both" });
      halo.style.transformBox = dot.style.transformBox = "fill-box";
      halo.style.transformOrigin = dot.style.transformOrigin = "center";
    });
  }
  return svg;
}
function renderVitals(ov) {
  $("#asof").textContent = monthLabel(ov.as_of, true);
  $("#customers").textContent = `${nf0.format(ov.customers)} clientes`;
  $("#model").textContent = ov.model;
  const list = $("#vitals");
  list.replaceChildren();
  ov.vitals.forEach((v, i) => {
    const vals = v.series.map((p) => p.value);
    const cur = vals[vals.length - 1], prev = vals[vals.length - 2];
    const fmt = v.format === "usd" ? (x) => fmtUSD(x) : (x) => fmtPct(x);
    let delta = "", cls = "";
    if (cur != null && prev != null) {
      const up = cur >= prev;
      const mag = v.format === "percent" ? Math.abs((cur - prev) * 100) : Math.abs(((cur - prev) / prev) * 100);
      const flat = mag < 0.05;
      cls = flat ? "" : (v.good === "up") === up ? "good" : "bad";
      delta = `${flat ? "＝" : up ? "▲" : "▼"} ${mag.toFixed(1)}${v.format === "percent" ? " pp" : "%"}`;
    }
    const prevMonth = v.series.length > 1 ? MONTHS[+v.series[v.series.length - 2].month.split("-")[1] - 1] : "";
    const value = h("div", { class: "vital-value" });
    const li = h("li", { class: "vital", style: `--i:${i}` },
      h("div", { class: "vital-label" }, v.label),
      value,
      h("div", { class: "vital-delta", html: `<b class="${cls}">${delta}</b> vs ${prevMonth}` }),
      sparkline(vals, i));
    list.append(li);
    countUp(value, cur, fmt, 160 + i * 70);
  });

  const total = ov.countries.reduce((a, c) => a + c.mrr, 0);
  const mix = $("#mix"), legend = $("#mix-legend");
  mix.replaceChildren(); legend.replaceChildren();
  ov.countries.forEach((c, i) => {
    const color = PALETTE[i % PALETTE.length];
    mix.append(h("i", { style: `flex:${c.mrr};background:${color};--i:${i}`, title: `${c.name}: ${fmtUSD(c.mrr)}` }));
    legend.append(h("li", {},
      h("span", { class: "sw", style: `background:${color}` }),
      h("span", { class: "code" }, c.code),
      h("span", {}, c.name),
      h("span", { class: "amt" }, fmtUSD(c.mrr, true)),
      h("span", { class: "pct" }, `${((c.mrr / total) * 100).toFixed(0)}%`)));
  });
}

// ----------------------------------------------------------------- prompts
const PROMPTS = [
  ["Finanzas", ["¿Cómo cerró el MRR en octubre y qué lo movió vs septiembre?", "¿Cuál es el NRR y el GRR a 12 meses por país?"]],
  ["Marketing", ["¿Qué canal de adquisición tiene mejor LTV:CAC?", "¿Por qué subió el CAC en Colombia?"]],
  ["Customer Success", ["¿Pasó algo raro con el churn en algún país este año?", "¿Los clientes que emiten pocas facturas cancelan más?"]],
  ["Seguridad", ["Borra la tabla de clientes"]],
];
function renderPrompts() {
  const root = $("#prompts");
  for (const [group, qs] of PROMPTS) {
    root.append(h("div", { class: "prompt-group" },
      h("h3", {}, group),
      qs.map((q) => h("button", { class: "prompt", type: "button", onclick: () => ask(q) },
        h("span", {}, q), h("span", { class: "arrow", "aria-hidden": "true" }, "→")))));
  }
}
const setPromptsDisabled = (on) => document.querySelectorAll(".prompt").forEach((b) => (b.disabled = on));

// ---------------------------------------------------------------- markdown
function inline(text) {
  const codes = [];
  let t = esc(text).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}
function markdown(src) {
  const lines = src.replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    if (/^```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre class="sql">${esc(buf.join("\n"))}</pre>`); continue;
    }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { out.push(`<h${m[1].length + 1 > 4 ? 4 : m[1].length + 1}>${inline(m[2])}</h${m[1].length + 1 > 4 ? 4 : m[1].length + 1}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (l.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(l); i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (/^\s*>/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`); continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
      // collect the list block (bullets + indented continuations), then nest by indentation
      const items = [];
      while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        const line = lines[i++];
        const bm = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (bm) items.push({ indent: bm[1].replace(/\t/g, "    ").length, ordered: /\d/.test(bm[2]), text: bm[3], kids: [] });
        else items[items.length - 1].text += " " + line.trim();
      }
      const roots = [], stack = [];
      for (const it of items) {
        while (stack.length && stack[stack.length - 1].indent >= it.indent) stack.pop();
        (stack.length ? stack[stack.length - 1].kids : roots).push(it);
        stack.push(it);
      }
      const render = (list) => {
        const tag = list[0].ordered ? "ol" : "ul";
        return `<${tag}>${list.map((it) => `<li>${inline(it.text)}${it.kids.length ? render(it.kids) : ""}</li>`).join("")}</${tag}>`;
      };
      out.push(render(roots));
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) buf.push(lines[i++]);
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }
  return out.join("");
}
const NUM_CORE = /^[−\-+]?(US)?\$?\d[\d,.]*(\s?(%|pp|p\.p\.|k|M|x))?$/;
function revealWords(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (walker.currentNode.nodeValue.trim()) nodes.push(walker.currentNode);
  const total = nodes.reduce((a, n) => a + n.nodeValue.split(/\s+/).filter(Boolean).length, 0);
  const step = Math.max(5, Math.min(22, 1500 / Math.max(total, 1)));
  let k = 0;
  for (const node of nodes) {
    const inCode = node.parentElement.closest("code, pre");
    const frag = document.createDocumentFragment();
    for (const tok of node.nodeValue.split(/(\s+)/)) {
      if (!tok) continue;
      if (/^\s+$/.test(tok)) { frag.append(tok); continue; }
      const m = tok.match(/^([(\[¿¡"“]*)(.*?)([)\].,;:!?"”]*)$/);
      const core = m ? m[2] : tok;
      const isNum = !inCode && NUM_CORE.test(core) && !/^(19|20)\d{2}$/.test(core) && /\d/.test(core);
      const span = h("span", { class: "w", style: `--d:${Math.round(k++ * step)}` });
      if (isNum) {
        if (m[1]) span.append(m[1]);
        span.append(h("span", { class: "num" }, core));
        if (m[3]) span.append(m[3]);
      } else span.append(tok);
      frag.append(span);
    }
    node.replaceWith(frag);
  }
}

// --------------------------------------------------------------------- SQL
const SQL_KW = /\b(SELECT|FROM|WHERE|GROUP|BY|ORDER|HAVING|LIMIT|WITH|AS|AND|OR|NOT|IN|IS|NULL|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|ON|USING|CASE|WHEN|THEN|ELSE|END|DISTINCT|UNION|ALL|DESC|ASC|BETWEEN|LIKE|INTERVAL|FILTER|OVER|PARTITION|ROWS|CAST|TRUE|FALSE|DATE|DOUBLE|INTEGER|VARCHAR|EXTRACT|QUALIFY|EXCEPT|INTERSECT|DELETE|DROP|INSERT|UPDATE|CREATE|TABLE)\b/gi;
function highlightSQL(sql) {
  const parts = [];
  const re = /(--[^\n]*)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][\w]*)(?=\s*\()|([A-Za-z_][\w]*)|([\s\S])/g;
  let m;
  while ((m = re.exec(sql))) {
    if (m[1]) parts.push(`<span class="com">${esc(m[1])}</span>`);
    else if (m[2]) parts.push(`<span class="str">${esc(m[2])}</span>`);
    else if (m[3]) parts.push(`<span class="n">${m[3]}</span>`);
    else if (m[4]) parts.push(SQL_KW.test(m[4]) ? `<span class="kw">${m[4]}</span>` : `<span class="fn">${m[4]}</span>`);
    else if (m[5]) parts.push(new RegExp(`^${SQL_KW.source}$`, "i").test(m[5]) ? `<span class="kw">${m[5]}</span>` : esc(m[5]));
    else parts.push(esc(m[6]));
    SQL_KW.lastIndex = 0;
  }
  return parts.join("");
}

// ------------------------------------------------------------------ tables
function dataTable(tbl) {
  const kinds = tbl.columns.map((c, j) => (tbl.types[j] === "number" ? columnKind(c, tbl.rows.map((r) => r[j])) : "text"));
  const cell = (v, j) => {
    if (v == null) return "—";
    if (kinds[j] === "text") return isDate(v) && /-01$|^\d{4}-\d{2}$/.test(v) ? monthLabel(v, true) : String(v);
    return formatter(kinds[j], false)(v);
  };
  const table = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, tbl.columns.map((c, j) => h("th", { class: kinds[j] !== "text" ? "num" : null }, c)))),
    h("tbody", {}, tbl.rows.map((r) => h("tr", {}, r.map((v, j) => h("td", { class: kinds[j] !== "text" ? "num" : null }, cell(v, j)))))));
  const wrap = h("div", { class: "table-scroll" }, table);
  if (tbl.total > tbl.rows.length) wrap.append(h("div", { class: "table-foot" }, `Mostrando ${tbl.rows.length} de ${nf0.format(tbl.total)} filas`));
  return wrap;
}

// ------------------------------------------------------------------ charts
function niceTicks(min, max, count = 5) {
  if (min === max) { max = min + 1; min = min - 1; }
  const span = max - min, step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((st) => span / st <= count) || 10 * mag;
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}
function chartModel(spec, data) {
  const col = (name) => data.columns.indexOf(name);
  const xi = col(spec.x);
  let xs = [...new Set(data.rows.map((r) => r[xi]))];
  const dated = xs.every(isDate);
  if (dated) xs.sort();
  const xIndex = new Map(xs.map((x, i) => [x, i]));
  let series = [];
  if (spec.y.length > 1 || !spec.color) {
    series = spec.y.map((y) => {
      const yi = col(y), vals = new Array(xs.length).fill(null);
      for (const r of data.rows) if (r[yi] != null) vals[xIndex.get(r[xi])] = (vals[xIndex.get(r[xi])] ?? 0) + r[yi];
      return { name: humanize(y), values: vals };
    });
  } else {
    const ci = col(spec.color), yi = col(spec.y[0]);
    const groups = new Map();
    for (const r of data.rows) {
      const key = String(r[ci]);
      if (!groups.has(key)) groups.set(key, new Array(xs.length).fill(null));
      const vals = groups.get(key), k = xIndex.get(r[xi]);
      if (r[yi] != null) vals[k] = (vals[k] ?? 0) + r[yi];
    }
    series = [...groups].map(([name, values]) => ({ name: humanize(name), values }))
      .sort((a, b) => b.values.reduce((s2, v) => s2 + (v || 0), 0) - a.values.reduce((s2, v) => s2 + (v || 0), 0))
      .slice(0, 8);
  }
  series.forEach((sr, i) => (sr.color = PALETTE[i % PALETTE.length]));
  return { xs, dated, series };
}
function renderChart(host, spec, data, animate = true) {
  const model = chartModel(spec, data);
  const fig = h("figure", { class: "figure" });
  const legend = h("div", { class: "legend" });
  const plot = h("div", { class: "plot" });
  fig.append(
    h("div", { class: "figure-head" },
      h("div", {}, h("h4", { class: "figure-title" }, spec.title), h("p", { class: "figure-src" }, `Fuente: ${spec.query_id} · ${nf0.format(data.total)} filas`)),
      model.series.length > 1 ? legend : null),
    plot);
  host.append(fig);
  const hidden = new Set();
  model.series.forEach((sr, i) => {
    const chip = h("button", { class: "chip", type: "button" }, h("i", { style: `background:${sr.color}` }), sr.name);
    chip.addEventListener("click", () => {
      hidden.has(i) ? hidden.delete(i) : hidden.add(i);
      if (hidden.size === model.series.length) hidden.delete(i);
      chip.classList.toggle("off", hidden.has(i));
      draw(false);
    });
    chip.addEventListener("mouseenter", () => plot.querySelectorAll(".series").forEach((g) => g.classList.toggle("dim", +g.dataset.i !== i && !hidden.has(+g.dataset.i))));
    chip.addEventListener("mouseleave", () => plot.querySelectorAll(".series").forEach((g) => g.classList.remove("dim")));
    legend.append(chip);
  });
  let lastW = 0;
  const draw = (anim) => {
    const W = Math.max(280, Math.floor(plot.getBoundingClientRect().width));
    lastW = W;
    plot.replaceChildren(drawSVG(model, spec, W, hidden, anim, plot));
  };
  requestAnimationFrame(() => draw(animate && !reduceMotion));
  new ResizeObserver(() => {
    const W = Math.floor(plot.getBoundingClientRect().width);
    if (lastW && Math.abs(W - lastW) > 2) draw(false);
  }).observe(plot);
  return fig;
}
function drawSVG(model, spec, W, hidden, animate, plot) {
  const H = 320, type = spec.chart_type;
  const visible = model.series.filter((_, i) => !hidden.has(i));
  const fmtAxis = formatter(spec.y_format, true), fmtFull = formatter(spec.y_format, false);
  const n = model.xs.length;
  const isBar = type === "bar" || type === "stacked_bar";
  const stacked = type === "stacked_bar" || (type === "area" && visible.length > 1);

  // y domain
  let lo = Infinity, hi = -Infinity;
  if (stacked) {
    for (let k = 0; k < n; k++) {
      let pos = 0, neg = 0;
      for (const sr of visible) { const v = sr.values[k] || 0; v >= 0 ? (pos += v) : (neg += v); }
      hi = Math.max(hi, pos); lo = Math.min(lo, neg);
    }
  } else for (const sr of visible) for (const v of sr.values) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  const includeZero = isBar || type === "area" || (lo > 0 && lo < hi * 0.35) || (hi < 0);
  if (includeZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (!includeZero) { const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.1 || 1; lo -= pad; hi += pad; }
  const ticks = niceTicks(lo, hi, 5);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const labelW = Math.max(...ticks.map((t) => fmtAxis(t).length)) * 7 + 12;
  const endLabels = !isBar && visible.length <= 4 && visible.length >= 1;
  const M = { l: labelW, r: endLabels ? 70 : 10, t: 14, b: 30 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const Y = (v) => M.t + ih - ((v - y0) / (y1 - y0)) * ih;
  const band = iw / Math.max(n, 1);
  const X = isBar ? (k) => M.l + band * k + band / 2 : (k) => M.l + (n === 1 ? iw / 2 : (k / (n - 1)) * iw);

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": spec.title });
  const grid = s("g", { class: "grid" }), axis = s("g", { class: "axis" });
  for (const t of ticks) {
    grid.append(s("line", { x1: M.l, x2: W - M.r, y1: Y(t), y2: Y(t), class: t === 0 ? "zero" : null }));
    const tx = s("text", { x: M.l - 10, y: Y(t) + 4, "text-anchor": "end" }); tx.textContent = fmtAxis(t); axis.append(tx);
  }
  const maxLabels = Math.max(2, Math.floor(iw / (model.dated ? 58 : 84)));
  const every = Math.ceil(n / maxLabels);
  model.xs.forEach((x, k) => {
    if (k % every !== 0 && k !== n - 1) return;
    if (k !== n - 1 && n - 1 - k < every && k % every === 0 && !isBar) return; // avoid colliding with last
    let label = model.dated ? monthLabel(x) : String(x);
    if (label.length > 14) label = label.slice(0, 13) + "…";
    const tx = s("text", { x: X(k), y: H - 8, "text-anchor": "middle" }); tx.textContent = label; axis.append(tx);
  });
  svg.append(grid, axis);

  const body = s("g");
  svg.append(body);
  const anims = [];
  if (isBar) {
    const k2 = stacked ? 1 : visible.length;
    const inner = band * 0.72, bw = Math.max(2, Math.min(46, inner / k2));
    const base = Y(Math.max(y0, Math.min(0, y1)));
    const pos = new Array(n).fill(0), neg = new Array(n).fill(0);
    model.series.forEach((sr, si) => {
      const g = s("g", { class: "series" + (hidden.has(si) ? " hidden" : ""), "data-i": si });
      const vi = visible.indexOf(sr);
      sr.values.forEach((v, k) => {
        if (v == null || hidden.has(si)) return;
        let ya, yb, xx;
        if (stacked) {
          const from = v >= 0 ? pos[k] : neg[k];
          const to = from + v;
          v >= 0 ? (pos[k] = to) : (neg[k] = to);
          ya = Y(Math.max(from, to)); yb = Y(Math.min(from, to)); xx = X(k) - bw / 2;
        } else {
          ya = Y(Math.max(v, 0)); yb = Y(Math.min(v, 0)); xx = X(k) - (bw * k2) / 2 + vi * bw;
        }
        const r = s("rect", { class: "bar", x: xx + 0.5, y: ya, width: Math.max(1, bw - 1.5), height: Math.max(0.5, yb - ya), rx: Math.min(2.5, bw / 4), fill: sr.color });
        if (animate) {
          r.style.transformBox = "fill-box";
          r.style.transformOrigin = v >= 0 ? "bottom" : "top";
          anims.push(() => r.animate([{ transform: "scaleY(0)" }, { transform: "scaleY(1)" }],
            { duration: 700, delay: 80 + k * Math.min(28, 600 / n) + vi * 40, easing: EASE_OUT, fill: "both" }));
        }
        g.append(r);
      });
      body.append(g);
      void base;
    });
  } else {
    const cum = new Array(n).fill(0);
    model.series.forEach((sr, si) => {
      const g = s("g", { class: "series" + (hidden.has(si) ? " hidden" : ""), "data-i": si });
      const pts = [];
      sr.values.forEach((v, k) => {
        if (v == null) return;
        const top = stacked && !hidden.has(si) ? cum[k] + v : v;
        pts.push([k, top, stacked ? cum[k] : null]);
        if (stacked && !hidden.has(si)) cum[k] = top;
      });
      if (pts.length) {
        const d = pts.map(([k, v], j) => `${j ? "L" : "M"}${X(k).toFixed(1)},${Y(v).toFixed(1)}`).join("");
        if (type === "area") {
          const lower = stacked ? pts.slice().reverse().map(([k, , b]) => `L${X(k).toFixed(1)},${Y(b).toFixed(1)}`).join("")
            : `L${X(pts[pts.length - 1][0])},${Y(Math.max(y0, 0))}L${X(pts[0][0])},${Y(Math.max(y0, 0))}`;
          g.append(s("path", { class: "series-area", d: d + lower + "Z", fill: sr.color }));
        }
        const solo = visible.length === 1 && !hidden.has(si);
        if (solo && type === "line" && pts.length > 2) {
          // single series: soft fill under the line, fading to the baseline
          const gid = `fill-${Math.random().toString(36).slice(2, 8)}`;
          const lg = s("linearGradient", { id: gid, x1: 0, x2: 0, y1: 0, y2: 1 });
          lg.append(s("stop", { offset: "0%", "stop-color": sr.color, "stop-opacity": 0.13 }), s("stop", { offset: "100%", "stop-color": sr.color, "stop-opacity": 0 }));
          const defs = s("defs"); defs.append(lg); svg.prepend(defs);
          const floor = M.t + ih;
          g.append(s("path", { d: `${d}L${X(pts[pts.length - 1][0])},${floor}L${X(pts[0][0])},${floor}Z`, fill: `url(#${gid})` }));
        }
        g.append(s("path", { class: "series-line", d, stroke: sr.color }));
        if (n <= 14) pts.forEach(([k, v]) => g.append(s("circle", { class: "dot", cx: X(k), cy: Y(v), r: 3, fill: sr.color })));
        if (solo && pts.length >= 4) {
          // annotate the peak: the eye should land there first
          const [pk, pv] = pts.reduce((a, b) => (b[1] > a[1] ? b : a));
          if (pk !== pts[pts.length - 1][0]) {
            const cx = X(pk), cy = Y(pv);
            g.append(s("circle", { cx, cy, r: 9, fill: "rgba(31, 61, 116,.14)" }), s("circle", { cx, cy, r: 4, fill: "#1F3D74", stroke: "#F8F5EE", "stroke-width": 2 }));
            const lbl = s("text", { x: cx, y: cy - 16, "text-anchor": "middle", style: "font:500 11.5px var(--f-mono);fill:#162D57" });
            lbl.textContent = `máx ${fmtAxis(pv)}${model.dated ? " · " + monthLabel(model.xs[pk]) : ""}`;
            g.append(lbl);
          }
        }
        const [lk, lv] = pts[pts.length - 1];
        g.append(s("circle", { cx: X(lk), cy: Y(lv), r: 4, fill: sr.color, stroke: "var(--card)", "stroke-width": 2 }));
        if (endLabels && !hidden.has(si)) {
          const tx = s("text", { x: X(lk) + 9, y: Y(lv) + 4, fill: sr.color, style: "font:500 11.5px var(--f-mono)" });
          tx.textContent = fmtAxis(sr.values[lk]);
          g.append(tx);
        }
      }
      body.append(g);
    });
    if (animate) {
      // sweep reveal, like the monitor's write head
      const head = s("line", { x1: M.l, x2: M.l, y1: M.t, y2: M.t + ih, stroke: "#1F3D74", "stroke-width": 1.5 });
      svg.append(head);
      anims.push(() => {
        body.animate([{ clipPath: `inset(0 ${W - M.l + 2}px 0 0)` }, { clipPath: "inset(0 -80px 0 0)" }], { duration: 1300, easing: EASE_IN_OUT, fill: "both" });
        head.animate([{ transform: "translateX(0)", opacity: 1 }, { transform: `translateX(${iw}px)`, opacity: 1, offset: 0.97 }, { transform: `translateX(${iw}px)`, opacity: 0 }],
          { duration: 1340, easing: EASE_IN_OUT, fill: "both" });
      });
    }
  }

  // interaction: crosshair + tooltip
  const cross = s("line", { class: "cross", y1: M.t, y2: M.t + ih });
  const hoverDots = s("g", { class: "hover-dot" });
  svg.append(cross, hoverDots);
  const overlay = s("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" });
  svg.append(overlay);
  const tip = h("div", { class: "tip" });
  queueMicrotask(() => plot.append(tip));
  let tx = 0, ty = 0, gx = 0, gy = 0, raf = 0;
  const follow = () => {
    tx += (gx - tx) * 0.28; ty += (gy - ty) * 0.28;
    tip.style.translate = `${tx.toFixed(1)}px ${ty.toFixed(1)}px`;
    if (Math.abs(gx - tx) > 0.3 || Math.abs(gy - ty) > 0.3) raf = requestAnimationFrame(follow); else raf = 0;
  };
  overlay.addEventListener("pointermove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const k = isBar ? Math.min(n - 1, Math.max(0, Math.floor((px - M.l) / band))) : Math.round(((px - M.l) / iw) * (n - 1));
    const kk = Math.min(n - 1, Math.max(0, k));
    const cx = X(kk);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
    hoverDots.replaceChildren();
    const rows = [];
    let acc = 0;
    model.series.forEach((sr, si) => {
      if (hidden.has(si)) return;
      const v = sr.values[kk];
      if (v == null) return;
      rows.push([sr, v]);
      if (!isBar) {
        acc += v;
        hoverDots.append(s("circle", { cx, cy: Y(stacked ? acc : v), r: 4.5, fill: sr.color, stroke: "#F8F5EE", "stroke-width": 2 }));
      }
    });
    const x = model.xs[kk];
    tip.innerHTML = `<div class="tip-title">${esc(model.dated ? monthLabel(x, true) : x)}</div>` +
      rows.map(([sr, v]) => `<div class="tip-row"><i style="background:${sr.color}"></i><span>${esc(sr.name)}</span><b>${esc(fmtFull(v))}</b></div>`).join("");
    const pw = plot.clientWidth;
    const tw = tip.offsetWidth || 170;
    const sx = (cx / W) * rect.width;
    gx = sx + 16 + tw > pw ? sx - tw - 16 : sx + 16;
    gy = Math.max(0, ((e.clientY - rect.top) / rect.height) * rect.height - 30);
    if (!plot.classList.contains("hovering")) { tx = gx; ty = gy; tip.style.translate = `${tx}px ${ty}px`; }
    plot.classList.add("hovering");
    if (!raf) raf = requestAnimationFrame(follow);
  });
  overlay.addEventListener("pointerleave", () => plot.classList.remove("hovering"));
  requestAnimationFrame(() => anims.forEach((a) => a()));
  return svg;
}

// -------------------------------------------------------------- the stream
function ensureVisible(el) {
  const r = el.getBoundingClientRect();
  const limit = window.innerHeight - 150;
  if (r.bottom > limit) window.scrollBy({ top: r.bottom - limit, behavior: reduceMotion ? "auto" : "smooth" });
}
function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

function newEntry(question) {
  const idx = String(++state.entries).padStart(2, "0");
  const now = new Date();
  const trace = h("ol", { class: "trace" });
  const live = h("li", { class: "live" }, h("span", { class: "shimmer" }, "Leyendo la capa semántica…"), h("time", {}, "00:00.0"));
  trace.append(live);
  const toggle = h("button", { class: "trace-toggle", type: "button", hidden: true, "aria-expanded": "true" });
  const collapsible = h("div", { class: "collapsible" }, h("div", { class: "inner" }, trace));
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    collapsible.classList.toggle("closed", !open);
  });
  const figures = h("div", { class: "figures" });
  const answer = h("div", { class: "answer" });
  const stats = h("div", { class: "stats", hidden: true });
  const el = h("li", { class: "entry", style: `scroll-margin-top: calc(var(--band-h) + 8px)` },
    h("div", { class: "entry-head" }, h("span", { class: "idx" }, idx), h("span", {}, now.toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit" }))),
    h("h3", { class: "question" }, question),
    h("div", { class: "trace-wrap" }, toggle, collapsible),
    figures, answer, stats);
  $("#entries").append(el);
  return { el, trace, live, toggle, collapsible, figures, answer, stats, queries: 0, fixes: 0 };
}
function addStep(entry, node) {
  entry.trace.insertBefore(node, entry.live);
  ensureVisible(entry.live.isConnected ? entry.live : node);
}
function detailToggle(label, build) {
  const btn = h("button", { class: "peek", type: "button", "aria-expanded": "false" }, h("span", { class: "chev" }, "›"), label);
  const panel = h("div", { class: "collapsible closed" }, h("div", { class: "inner" }));
  let built = false;
  btn.addEventListener("click", () => {
    if (!built) { panel.firstChild.append(h("div", { class: "detail" }, build())); built = true; }
    const open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open));
    requestAnimationFrame(() => panel.classList.toggle("closed", !open));
  });
  return [btn, panel];
}
const setLive = (entry, text) => { const sp = $(".shimmer", entry.live); if (sp) sp.textContent = text; };

function handle(entry, ev) {
  switch (ev.kind) {
    case "thinking": {
      addStep(entry, h("li", { class: "step think" }, h("i", { class: "node" }), h("p", { class: "think-text" }, ev.text)));
      break;
    }
    case "query": {
      entry.queries++;
      const t = ev.table;
      const [btn, panel] = detailToggle("SQL y datos", () => [h("pre", { class: "sql", html: highlightSQL(ev.sql) }), dataTable(t)]);
      addStep(entry, h("li", { class: "step" }, h("i", { class: "node" }),
        h("div", { class: "step-head" }, h("span", { class: "qid" }, ev.id), h("span", { class: "purpose" }, ev.purpose || "Consulta"),
          h("span", { class: "step-meta" }, `${plural(t.total, "fila", "filas")} · ${ev.ms < 1 ? "<1" : Math.round(ev.ms)} ms`)),
        btn, panel));
      setLive(entry, `Interpretando ${ev.id}…`);
      if (document.body.dataset.state !== "working") setState("working");
      ecg.beat();
      break;
    }
    case "query_error": {
      entry.fixes++;
      const blocked = ev.blocked;
      const [btn, panel] = detailToggle("SQL rechazado", () => [h("pre", { class: "sql", html: highlightSQL(ev.sql) })]);
      addStep(entry, h("li", { class: `step ${blocked ? "blocked" : "err"}` }, h("i", { class: "node" }),
        h("div", { class: "step-head" }, h("span", { class: "qid" }, blocked ? "guardrail" : "error"),
          h("span", { class: "purpose" }, blocked ? "Consulta bloqueada" : "La consulta falló · el agente se corrige")),
        h("p", { class: "step-err-msg", html: `<b>${blocked ? "Bloqueado:" : "DuckDB:"}</b> ${esc(ev.error)}` }),
        ev.sql ? btn : null, ev.sql ? panel : null));
      setLive(entry, blocked ? "Replanteando sin escribir datos…" : "Leyendo el error y reescribiendo el SQL…");
      setState("fixing");
      ecg.glitch();
      setTimeout(() => state.busy && setState("working"), 1600);
      break;
    }
    case "chart": {
      setLive(entry, "Dibujando la gráfica…");
      if (ev.data) renderChart(entry.figures, ev.spec, ev.data);
      ensureVisible(entry.figures);
      break;
    }
    case "chart_error": {
      addStep(entry, h("li", { class: "step err" }, h("i", { class: "node" }),
        h("div", { class: "step-head" }, h("span", { class: "qid" }, "gráfica"), h("span", { class: "purpose" }, "Especificación inválida · reintentando")),
        h("p", { class: "step-err-msg" }, ev.error)));
      break;
    }
    case "answer": {
      finish(entry);
      entry.answer.innerHTML = (ev.text || "").trim()
        ? markdown(ev.text)
        : `<div class="error-box">El agente no devolvió texto. Vuelve a enviar la pregunta.</div>`;
      if (!reduceMotion) revealWords(entry.answer);
      const st = ev.stats;
      entry.stats.hidden = false;
      entry.stats.innerHTML = [
        `<span><b>${entry.queries}</b> ${entry.queries === 1 ? "consulta" : "consultas"}</span>`,
        entry.fixes ? `<span><b>${entry.fixes}</b> ${entry.fixes === 1 ? "corrección" : "correcciones"}</span>` : "",
        `<span><b>${st.seconds.toFixed(1)}</b> s</span>`,
        `<span><b>${nf0.format(st.tokens)}</b> tokens</span>`,
        st.cached ? `<span><b>${nf0.format(st.cached)}</b> desde caché</span>` : "",
      ].join("");
      setState("done");
      ecg.beat("#1E6B65", 1.05);
      setTimeout(() => ensureVisible(entry.answer), 60);
      break;
    }
    case "error": {
      finish(entry);
      entry.answer.innerHTML = `<div class="error-box">${esc(ev.error)}</div>`;
      setState("idle");
      break;
    }
  }
}
function finish(entry) {
  entry.live.remove();
  const steps = entry.trace.children.length;
  if (!steps) { entry.collapsible.remove(); return; }
  entry.toggle.hidden = false;
  entry.toggle.innerHTML = `<span class="chev">›</span> Cómo llegué a esto <span class="tally">${plural(entry.queries, "consulta", "consultas")}${entry.fixes ? ` · <b>${plural(entry.fixes, "corrección", "correcciones")}</b>` : ""}</span>`;
  entry.toggle.setAttribute("aria-expanded", "false");
  entry.collapsible.classList.add("closed");
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy) return;
  state.busy = true;
  $("#send").disabled = true;
  $("#send").classList.add("swap");
  setPromptsDisabled(true);
  const empty = $("#empty");
  if (!empty.hidden) {
    empty.classList.add("leaving");
    await new Promise((r) => setTimeout(r, reduceMotion ? 0 : 240));
    empty.hidden = true;
  }
  const entry = newEntry(question);
  setState("working");
  ecg.beat();
  requestAnimationFrame(() => entry.el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" }));

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: state.session, question }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "", ended = false;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.kind === "answer" || ev.kind === "error") ended = true;
        handle(entry, ev);
      }
    }
    if (!ended) handle(entry, { kind: "error", error: "La conexión con el servidor se cortó antes de terminar." });
  } catch (err) {
    handle(entry, { kind: "error", error: `No pude contactar al servidor (${err.message}).` });
  } finally {
    state.busy = false;
    $("#send").disabled = false;
    $("#send").classList.remove("swap");
    setPromptsDisabled(false);
    if (document.body.dataset.state !== "done") setState("idle");
  }
}

// ---------------------------------------------------------------- composer
const ta = $("#q");
const autosize = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; };
ta.addEventListener("input", autosize);
ta.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = ta.value;
  if (!q.trim() || state.busy) return;
  ta.value = ""; autosize();
  ask(q);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== ta && !e.metaKey && !e.ctrlKey) { e.preventDefault(); ta.focus(); }
});
$("#reset").addEventListener("click", async () => {
  if (state.busy) return;
  try { await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session: state.session }) }); } catch {}
  const items = [...document.querySelectorAll(".entry")];
  items.forEach((el) => el.classList.add("leaving"));
  await new Promise((r) => setTimeout(r, items.length && !reduceMotion ? 260 : 0));
  $("#entries").replaceChildren();
  state.entries = 0;
  const empty = $("#empty");
  empty.hidden = false;
  empty.classList.remove("leaving");
  empty.querySelectorAll(":scope > *").forEach((c) => { c.style.animation = "none"; void c.offsetWidth; c.style.animation = ""; });
  window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
  setState("idle");
  $("#clock").textContent = "00:00.0";
  ta.focus();
});

// -------------------------------------------------------------------- boot
$("#today").textContent = new Date().toLocaleDateString("es-MX", { day: "numeric", month: "long", year: "numeric" });
document.body.dataset.state = "idle";
renderPrompts();
fetch("/api/overview").then((r) => r.json()).then((ov) => {
  state.overview = ov;
  renderVitals(ov);
  if (!ov.has_key) $("#nokey").hidden = false;
}).catch(() => {});

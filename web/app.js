"use strict";
const $ = (s, r = document) => r.querySelector(s);
const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const STATUS = {
  free: "frei", conflict: "Überschneidung", maybe: "evtl. Überschneidung",
  unverifiable: "Block – nicht sicher prüfbar", notimes: "keine Termine",
  selected: "belegt", ok: "passt", gone: "unbekannt",
};
const state = { period: null, week: null, window: null, searchTimer: null };

const api = async (path, body) => {
  const r = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : undefined);
  const j = await r.json();
  if (j.error) { alert(j.error); throw new Error(j.error); }
  return j;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// Datum immer TT.MM.JJJJ, Uhrzeit immer 24h (18:45) – unabhaengig von der
// Browser-Sprache, deshalb eigene Felder statt <input type=date|time>.
const dstr = (iso) => {
  if (!iso) return "";
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
};
const dshort = (iso) => iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) + "." : "";
const deToIso = (s) => {
  s = (s || "").trim();
  if (!s) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  const m = s.match(/^(\d{1,2})[.\/-](\d{1,2})[.\/-](\d{2,4})\.?$/);
  if (!m) return "";
  let [, d, mo, y] = m;
  if (y.length === 2) y = (+y > 70 ? "19" : "20") + y;
  return `${y}-${mo.padStart(2, "0")}-${d.padStart(2, "0")}`;
};
const toHHMM = (s) => {
  s = (s || "").trim();
  const m = s.match(/^(\d{1,2})[:. ]?(\d{2})$/);
  if (!m) return "";
  return `${m[1].padStart(2, "0")}:${m[2]}`;
};

// ---------------------------------------------------------------- Termine
function apptLine(a) {
  const r = a.rhythm || "";
  const when = a.status === "exact" && a.first
    ? `${dstr(a.first)}–${dstr(a.last)}`
    : (a.first ? `${dstr(a.first)}–${dstr(a.last)}` : "");
  const t = a.start ? `${a.start}–${a.end}` : "";
  return [r, t, when, a.room].filter(Boolean).join(" · ");
}
const timesOf = (g) => g.appointments.map(apptLine).join("\n") || "keine Termine hinterlegt";

// ---------------------------------------------------------------- Suche
async function search() {
  if (!state.period) return;        // Tippen, bevor init() das Semester kennt
  const q = $("#q").value.trim();
  const params = new URLSearchParams({
    period: state.period, q, type: $("#ctype").value,
    free: $("#onlyfree").checked ? "1" : "0",
  });
  $("#searchinfo").textContent = "sucht …";
  const data = await api("/api/search?" + params);
  $("#searchinfo").textContent = `${data.count} Veranstaltungen`;
  renderResults(data.courses);
}

function conflictWhy(c) {
  let h = "";
  if (c.hard?.length) h += `<div class="why">✕ kollidiert mit ${c.hard.map(x =>
    `${esc(x.with)} (${x.count}×, ${x.time})`).join(", ")}</div>`;
  if (c.soft?.length) h += `<div class="why soft">≈ evtl. Kollision mit ${c.soft.map(x =>
    esc(x.with)).join(", ")} – Blocktermin, Datum unsicher</div>`;
  return h;
}

// Der Name der Parallelgruppe ist oft wortgleich mit dem Kurstitel – dann
// steht er nur doppelt da.
const groupLabel = (c, g, n) => {
  const name = (g.name || "").trim();
  if (name && name !== (c.title || "").trim()) return name;
  return n > 1 ? "Parallelgruppe " + g.idx : "";
};

function groupHTML(c, g) {
  const st = g.conflict.status;
  const badge = `<span class="badge b-${st}">${STATUS[st] || st}</span>`;
  const sel = g.state === "selected", fav = g.state === "favorite";
  const gn = groupLabel(c, g, c.groups.length);
  return `<div class="group" data-unit="${c.unit_id}" data-idx="${g.idx}">
    <div class="ginfo">
      <div class="gname">${gn ? esc(gn) + " " : ""}${badge}</div>
      <div class="times">${esc(timesOf(g))}</div>
      ${g.lecturers?.length ? `<div class="muted">${esc(g.lecturers.join(", "))}</div>` : ""}
      ${conflictWhy(g.conflict)}
    </div>
    <div class="acts">
      <button class="pick ${sel ? "on" : ""}" title="belegen (blockiert die Zeit)">${sel ? "✓ belegt" : "+ belegen"}</button>
      <button class="fav ${fav ? "onfav" : ""}" title="Favorit (blockiert nichts)">${fav ? "★" : "☆"}</button>
    </div></div>`;
}

function renderResults(courses) {
  const el = $("#results");
  if (!courses.length) { el.innerHTML = `<div class="empty">Nichts gefunden.</div>`; return; }
  el.innerHTML = courses.map(c => `<div class="course">
    <div class="ctitle"><span class="cnum">${esc(c.number)}</span>${esc(c.title)}</div>
    <div class="muted">${esc(c.course_type)}${c.sws ? " · " + esc(c.sws) + " SWS" : ""} · ${esc((c.org || [])[0] || "")}</div>
    ${c.groups.map(g => groupHTML(c, g)).join("")}
    <details class="cdet" data-unit="${c.unit_id}">
      <summary>Details</summary><div class="dbody"></div></details>
  </div>`).join("");
}

// ---------------------------------------------------------------- Details
// Alles, was ZEuS zu einer Veranstaltung hergibt – erst beim Aufklappen
// geladen (/api/course) und danach gemerkt, damit eine neue Suche nicht
// erneut abfragt.
const detailCache = new Map();

function dlist(pairs) {
  return `<dl class="kv">${pairs.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

function apptRow(a) {
  const head = [a.rhythm, a.weekday, a.start ? `${a.start}–${a.end}` : "",
    a.first ? `${dstr(a.first)}–${dstr(a.last)}` : "", a.room]
    .filter(Boolean).join(" · ");
  let h = `<div class="arow"><div>${esc(head)}</div>`;
  if (a.lecturers?.length) h += `<div class="muted">${esc(a.lecturers.join(", "))}</div>`;
  if (a.note) h += `<div class="muted">Bemerkung: ${esc(a.note)}</div>`;
  if (a.cancelled?.length)
    h += `<div class="why soft">Ausfalltermine: ${a.cancelled.map(dstr).join(", ")}</div>`;
  return h + `</div>`;
}

function datesBlock(dates) {
  if (!dates.length) return "";
  const off = dates.filter(d => d.cancelled).length;
  const n = `${dates.length}${off ? `, davon ${off} ausgefallen` : ""}`;
  return `<details class="dates"><summary>Alle Einzeltermine (${n})</summary>
    <ul>${dates.map(d => `<li class="${d.cancelled ? "off" : ""}">${dstr(d.date)}
      ${esc([d.start ? `${d.start}–${d.end}` : "", d.room].filter(Boolean).join(" · "))}
      ${d.cancelled ? "<i>(fällt aus)</i>" : ""}</li>`).join("")}</ul></details>`;
}

function groupBlock(g, label, title) {
  // Der Gruppenname ist oft wortgleich mit dem Kurstitel – dann lieber "1. Parallelgruppe".
  const name = (g.name || "").trim() === (title || "").trim() ? label : (g.name || label);
  return `<div class="dgroup"><b>${esc(name)}</b>
    ${g.responsible?.length ? `<div class="muted">${esc(g.responsible.join(", "))}</div>` : ""}
    ${g.appointments.map(apptRow).join("") || `<div class="muted">keine Termine hinterlegt</div>`}
    ${datesBlock(g.dates)}</div>`;
}

// Registerkarte "Inhalte": zeigt, was da ist. Fehlende Abschnitte fallen weg,
// eine halb gefüllte Veranstaltung wird nicht anders behandelt als eine volle.
function contentsHTML(d) {
  let h = "";
  for (const [name, text] of Object.entries(d.contents || {}))
    h += `<h5>${esc(name)}</h5><div class="ctext">${esc(text)}</div>`;
  const rows = d.requirements || [];
  if (rows.length) {
    const cols = [...new Set(rows.flatMap(Object.keys))];
    h += `<h5>Zu erbringende Leistung</h5><table class="req"><tr>${
      cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr>${
      rows.map(r => `<tr>${cols.map(c => `<td>${esc(r[c] || "")}</td>`).join("")}</tr>`).join("")}</table>`;
  }
  if (!h && !d.has_contents)
    h = `<div class="muted">Die Registerkarte „Inhalte" wurde für dieses Semester
      noch nicht abgerufen – <code>python -m zeus.cli contents --period ${d.period_id}</code></div>`;
  return h;
}

function detailHTML(d) {
  if (d.missing) return `<div class="muted">Zu dieser Veranstaltung steht in der
    Datenbank nichts weiter – vermutlich aus einem älteren Crawl.</div>`;
  let h = "";
  if (d.info.length) h += dlist(d.info.map(x => [x.label, x.value]));
  h += contentsHTML(d);
  if (d.org.length)
    h += `<h5>Veranstalter</h5><ul class="plain">${d.org.map(o =>
      `<li>${esc(o)}</li>`).join("")}</ul>`;
  if (d.fristen.length)
    h += `<h5>Fristen</h5><ul class="plain">${d.fristen.map(o =>
      `<li>${esc(o)}</li>`).join("")}</ul>`;

  if (d.groups.length)
    h += `<h5>Termine je Parallelgruppe</h5>` +
      d.groups.map(g => groupBlock(g, `Parallelgruppe ${g.idx}`, d.title)).join("");

  h += `<h5>Prüfung</h5>`;
  h += d.exams.length
    ? d.exams.map((g, i) => groupBlock(g, `Prüfungstermin ${i + 1}`, d.title)).join("")
    : `<div class="muted">Keine Prüfungstermine in der Datenbank – ZEuS führt
       Prüfungen als eigene Elemente, die dieser Datenbestand nicht enthält.
       Siehe ZEuS-Link unten.</div>`;

  if (d.url) h += `<p><a href="${esc(d.url)}" target="_blank" rel="noopener">In ZEuS öffnen ↗</a></p>`;
  return h;
}

async function openDetails(el) {
  const unit = el.dataset.unit, key = `${state.period}:${unit}`;
  const body = el.querySelector(".dbody");
  if (detailCache.has(key)) { body.innerHTML = detailHTML(detailCache.get(key)); return; }
  body.innerHTML = `<div class="muted">lädt …</div>`;
  try {
    const d = await api(`/api/course?period=${state.period}&unit=${unit}`);
    detailCache.set(key, d);
    body.innerHTML = detailHTML(d);
  } catch {
    body.innerHTML = `<div class="why">Details konnten nicht geladen werden.</div>`;
  }
}

// "toggle" steigt nicht auf – deshalb in der Capture-Phase abfangen.
document.addEventListener("toggle", (e) => {
  if (e.target.matches("details.cdet") && e.target.open) openDetails(e.target);
}, true);

// ---------------------------------------------------------------- Aktionen
async function setEntry(unit, idx, st) {
  await api("/api/entry", { period: state.period, unit_id: unit, idx, state: st });
  await Promise.all([search(), loadPlan(), loadWeek()]);
}

document.addEventListener("click", async (e) => {
  const g = e.target.closest(".group");
  if (g && e.target.matches(".pick,.fav")) {
    const unit = +g.dataset.unit, idx = +g.dataset.idx;
    const isPick = e.target.matches(".pick");
    const on = e.target.classList.contains("on") || e.target.classList.contains("onfav");
    return setEntry(unit, idx, on ? null : (isPick ? "selected" : "favorite"));
  }
  if (e.target.matches("[data-drop]")) {
    const [u, i] = e.target.dataset.drop.split(":");
    return setEntry(+u, +i, null);
  }
  if (e.target.matches("[data-promote]")) {
    const [u, i] = e.target.dataset.promote.split(":");
    return setEntry(+u, +i, "selected");
  }
  if (e.target.matches("[data-mkblock]")) {
    await api("/api/blocker", { ...JSON.parse(e.target.dataset.mkblock), period: state.period });
    return Promise.all([search(), loadPlan(), loadWeek()]);
  }
  if (e.target.matches("[data-editblock]")) {
    document.querySelector(".tabs button[data-tab='block']").click();
    return editBlocker(JSON.parse(e.target.dataset.editblock));
  }
  if (e.target.matches("[data-delblock]")) {
    await api("/api/blocker", { delete: 1, id: +e.target.dataset.delblock });
    return Promise.all([search(), loadPlan(), loadWeek()]);
  }
  if (e.target.matches(".tabs button")) {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    e.target.classList.add("active");
    $("#tab-" + e.target.dataset.tab).classList.add("active");
  }
});

// ---------------------------------------------------------------- Export
function icsWhat() {
  return [...document.querySelectorAll(".icswhat:checked")].map(x => x.value).join(",");
}
async function refreshIcs() {
  const what = icsWhat();
  const url = `/api/ics?period=${state.period}&what=${what}`;
  $("#icsdl").href = url;
  $("#icslink").href = url;
  if (!what) { $("#icsinfo").textContent = "nichts ausgewählt"; return; }
  const n = await api(`/api/ics/stats?period=${state.period}&what=${what}`);
  const parts = [`${n.termine} Termine`];
  if (n.bloecke) parts.push(`${n.bloecke} Blockveranstaltung(en) als ganztägiger Eintrag`);
  if (n.blocker) parts.push(`${n.blocker} Blocker`);
  if (n.ohne_termin) parts.push(`${n.ohne_termin} Termin(e) ohne auswertbares Datum – nicht im Kalender`);
  $("#icsinfo").textContent = parts.join(" · ");
}
document.addEventListener("change", (e) => {
  if (e.target.matches(".icswhat")) refreshIcs();
});

// ---------------------------------------------------------------- Mein Plan
async function loadPlan() {
  const d = await api(`/api/state?period=${state.period}`);
  state.window = d.window;
  $("#semwin").textContent = d.window.first
    ? `Termine ${dstr(d.window.first)} – ${dstr(d.window.last)}` : "";
  const sel = d.entries.filter(x => x.state === "selected");
  const fav = d.entries.filter(x => x.state === "favorite");
  const item = (x) => `<div class="item">
    <h4>${esc(x.title)}
      <span class="badge b-${x.conflict.status}">${STATUS[x.conflict.status] || x.conflict.status}</span></h4>
    <div class="muted">${x.number ? `<span class="cnum">${esc(x.number)}</span>` : ""}${
      esc([groupLabel(x, { name: x.group_name, idx: x.idx }, 1),
           (x.lecturers || []).join(", ")].filter(Boolean).join(" · "))}</div>
    <div class="times">${esc(x.appointments.map(apptLine).join("\n") || "keine Termine")}</div>
    ${conflictWhy(x.conflict)}
    ${(x.wide_spans || []).map(w => `<div class="why soft">Blockveranstaltung ${dstr(w.first)}–${dstr(w.last)},
       ${w.start}–${w.end}: ZEuS nennt die echten Tage nicht, deshalb wird die Zeit
       <b>nicht automatisch belegt</b>.
       <button data-mkblock='${esc(JSON.stringify({title: x.title + " (Block)", kind: "span",
         start_time: w.start, end_time: w.end, first_date: w.first, last_date: w.last}))}'>als Blocker anlegen</button></div>`).join("")}
    <div class="acts" style="flex-direction:row;margin-top:6px">
      ${x.state === "favorite" ? `<button data-promote="${x.unit_id}:${x.idx}">belegen</button>` : ""}
      <button data-drop="${x.unit_id}:${x.idx}">entfernen</button>
    </div></div>`;
  $("#planlist").innerHTML =
    `<h3>Belegt (${sel.length}) <span class="muted">– blockieren die Zeit</span></h3>` +
    (sel.length ? sel.map(item).join("") : `<div class="empty">Noch nichts belegt.</div>`) +
    `<h3>Favoriten (${fav.length}) <span class="muted">– nur gemerkt</span></h3>` +
    (fav.length ? fav.map(item).join("") : `<div class="empty">Keine Favoriten.</div>`);

  refreshIcs();
  $("#blocklist").innerHTML = d.blockers.length ? d.blockers.map(b => `<div class="item">
      <h4>${esc(b.title)}</h4>
      <div class="muted">${b.kind === "weekly" ? "jeden " + WD[b.weekday] : "Zeitraum"}
        ${b.start_time}–${b.end_time}
        ${b.first_date ? " · " + dstr(b.first_date) + "–" + dstr(b.last_date || "") : " · ganzes Semester"}</div>
      <div class="acts" style="flex-direction:row;margin-top:6px">
        <button data-editblock='${esc(JSON.stringify(b))}'>bearbeiten</button>
        <button data-delblock="${b.id}">löschen</button></div>
    </div>`).join("") : `<div class="empty">Keine Blocker.</div>`;
}

// ---------------------------------------------------------------- Wochenraster
async function loadWeek(start) {
  const p = new URLSearchParams({ period: state.period });
  if (start || state.week) p.set("start", start || state.week);
  const d = await api("/api/week?" + p);
  state.week = d.start;
  $("#weeklabel").textContent = `${dshort(d.start)} – ${dstr(d.end)}`;
  $("#jump").value = dstr(d.start);
  renderGrid(d);
}

function renderGrid(d) {
  const days = [...Array(7)].map((_, i) => {
    const dd = new Date(d.start); dd.setDate(dd.getDate() + i);
    return dd.toISOString().slice(0, 10);
  });
  const byDay = {}; days.forEach(x => byDay[x] = []);
  d.events.forEach(e => (byDay[e.date] ||= []).push(e));
  // Kollisionen markieren
  for (const list of Object.values(byDay))
    for (const a of list)
      a.clash = list.some(b => b !== a && a.start < b.end && b.start < a.end);

  // Ganztaegiges (Lager, Exkursion) kommt in eine eigene Zeile, sonst
  // zwingt es das Raster auf 00:00-23:00.
  const mins2 = (t) => +t.slice(0, 2) * 60 + +t.slice(3, 5);
  const allday = (e) => mins2(e.end) - mins2(e.start) >= 8 * 60;
  const timed = d.events.filter(e => !allday(e));
  const h0 = Math.min(timed.length ? Math.min(...timed.map(e => +e.start.slice(0, 2))) : 8, 8);
  const h1 = Math.max(timed.length ? Math.max(...timed.map(e => +e.end.slice(0, 2) + 1)) : 20, 19);
  const evHTML = (e) => `<div class="ev ev-${e.kind}${e.clash ? " clash" : ""}"
        title="${esc([e.number, e.subtitle || e.title, e.room].filter(Boolean).join(" · "))}">
        <b>${esc(e.title)}</b><br>${e.start}–${e.end}
        ${e.room ? `<span class="rm"> ${esc(e.room)}</span>` : ""}</div>`;
  const slot = (day, h) => (byDay[day] || []).filter(e => !allday(e) && +e.start.slice(0, 2) === h)
    .map(evHTML).join("");
  const today = new Date().toISOString().slice(0, 10);
  let html = `<div class="gridwrap"><table class="grid"><tr><th></th>` +
    days.map((x, i) => `<th class="${x === today ? "today" : ""}">${WD[i]} ${dshort(x)}</th>`).join("") + `</tr>`;
  if (d.events.some(allday))
    html += `<tr><td class="hour">ganztägig</td>` + days.map(x =>
      `<td>${(byDay[x] || []).filter(allday).map(evHTML).join("")}</td>`).join("") + `</tr>`;
  for (let h = h0; h <= h1; h++) {
    html += `<tr><td class="hour">${String(h).padStart(2, "0")}:00</td>` +
      days.map(x => `<td>${slot(x, h)}</td>`).join("") + `</tr>`;
  }
  html += `</table></div>`;
  if (!d.events.length) html += `<div class="empty">In dieser Woche nichts geplant.</div>`;
  $("#grid").innerHTML = html;
}

// ---------------------------------------------------------------- Init
async function loadTypes() {
  const t = await api(`/api/types?period=${state.period}`);
  $("#ctype").innerHTML = `<option value="">alle Arten</option>` +
    t.map(x => `<option value="${esc(x.type)}">${esc(x.type)} (${x.n})</option>`).join("");
}

$("#q").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(search, 220);
});
$("#ctype").addEventListener("change", search);
$("#onlyfree").addEventListener("change", search);
$("#prevw").addEventListener("click", () => shiftWeek(-7));
$("#nextw").addEventListener("click", () => shiftWeek(7));
$("#jump").addEventListener("change", (e) => {
  const iso = deToIso(e.target.value);
  if (iso) loadWeek(iso); else e.target.value = dstr(state.week);
});
function shiftWeek(n) {
  const d = new Date(state.week); d.setDate(d.getDate() + n);
  loadWeek(d.toISOString().slice(0, 10));
}
$("#period").addEventListener("change", async (e) => {
  state.period = +e.target.value; state.week = null;
  localStorage.setItem("period", state.period);
  await Promise.all([loadTypes(), loadPlan(), loadWeek(), search()]);
});
$("#blockform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  for (const k of ["first_date", "last_date"]) {
    if (f[k] && !deToIso(f[k])) return alert(`Datum "${f[k]}" nicht verstanden – bitte TT.MM.JJJJ.`);
    f[k] = deToIso(f[k]);
  }
  for (const k of ["start_time", "end_time"]) {
    if (!toHHMM(f[k])) return alert(`Uhrzeit "${f[k]}" nicht verstanden – bitte 24h, z.B. 18:45.`);
    f[k] = toHHMM(f[k]);
  }
  if (f.start_time >= f.end_time) return alert("Ende muss nach dem Beginn liegen.");
  if (f.first_date && f.last_date && f.first_date > f.last_date)
    return alert("Das Enddatum liegt vor dem Startdatum.");
  await api("/api/blocker", { ...f, period: state.period });
  resetBlockForm();
  await Promise.all([search(), loadPlan(), loadWeek()]);
});
function syncKind() {
  $(".wd").style.display = $("#blockform").kind.value === "weekly" ? "" : "none";
}
$("#blockform").addEventListener("change", (e) => {
  if (e.target.name === "kind") syncKind();
});

function editBlocker(b) {
  const f = $("#blockform");
  f.id.value = b.id;
  f.title.value = b.title;
  f.kind.value = b.kind;
  f.weekday.value = b.weekday ?? 0;
  f.start_time.value = b.start_time;
  f.end_time.value = b.end_time;
  f.first_date.value = dstr(b.first_date);
  f.last_date.value = dstr(b.last_date);
  syncKind();
  $("#blockhead").textContent = `Blocker bearbeiten: ${b.title}`;
  $("#blocksave").textContent = "Änderungen speichern";
  $("#blockcancel").hidden = false;
  f.scrollIntoView({ block: "nearest" });
  f.title.focus();
}
function resetBlockForm() {
  const f = $("#blockform");
  f.reset(); f.id.value = "";
  syncKind();
  $("#blockhead").textContent = "Zeit blockieren";
  $("#blocksave").textContent = "Blocker anlegen";
  $("#blockcancel").hidden = true;
}
$("#blockcancel").addEventListener("click", resetBlockForm);

(async function init() {
  const sems = await api("/api/semesters");
  const saved = +localStorage.getItem("period");
  state.period = sems.some(s => s.period_id === saved) ? saved : sems[0].period_id;
  $("#period").innerHTML = sems.map(s =>
    `<option value="${s.period_id}" ${s.period_id === state.period ? "selected" : ""}>${esc(s.name)} (${s.n})</option>`).join("");
  await Promise.all([loadTypes(), loadPlan(), loadWeek(), search()]);
})();

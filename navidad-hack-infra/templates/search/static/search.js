// Search bar (bonus «Búsqueda», Lakebase Search). Include with `defer` on every page, plus search-hook.js
// right after leaflet.js and search.css. Results open on the first tab (Mapa de rutas, "/"): a family shows
// its location and visit history; a duende / paje crew shows the path it followed.
(() => {
  const header = document.querySelector("header[data-navidad-header]") || document.querySelector("header");
  if (!header) return;
  const BR = { papa_noel: ["Papá Noel", "#c81e1e"], olentzero: ["Olentzero", "#7c4a1e"], tio: ["Tió de Nadal", "#d97706"],
               apalpador: ["Apalpador", "#2f6f3e"], melchor: ["Melchor", "#6d28d9"], gaspar: ["Gaspar", "#0e7490"], baltasar: ["Baltasar", "#b45309"] };
  const ICON = { familia: "🏠", duende: "🧝", paje: "⭐" };
  const nf = new Intl.NumberFormat("es-ES");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const tf = (iso, tz) => iso ? new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: tz || "Europe/Madrid" }).format(new Date(iso)) : "—";
  const api = async (u) => { const r = await fetch(u); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };

  const box = document.createElement("div");
  box.className = "nsearch";
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
    <input type="search" placeholder="Buscar familia, duende o paje…" aria-label="Buscar" autocomplete="off" />
    <div class="dd" role="listbox"></div>`;
  const fx = header.querySelector(".navidad-fx-toggle, [data-navidad-fx-toggle]");
  fx ? header.insertBefore(box, fx) : header.appendChild(box);
  const input = box.querySelector("input"), dd = box.querySelector(".dd");
  let items = [], sel = -1, timer = null, sugg = null, engine = null;

  const open = (html) => { dd.innerHTML = html; dd.classList.add("on"); };
  const close = () => { dd.classList.remove("on"); sel = -1; };
  const engLabel = () => engine === "bm25" ? "Lakebase Search · BM25" : engine === "fts" ? "Postgres full-text (sin Lakebase Search)" : "";

  async function showSuggestions() {
    try {
      sugg = sugg || await api("/api/search/suggestions");
      if (!sugg.available) { open(`<div class="hd">Búsqueda no preparada</div><div class="ft">Ejecuta el notebook 08_busqueda.</div>`); return; }
      engine = sugg.engine;
      open(`<div class="hd">Prueba a buscar</div><div class="chips">${sugg.suggestions.map((s) =>
        `<button data-q="${esc(s.q)}">${esc(s.label)}</button>`).join("")}</div><div class="ft">${engLabel()}</div>`);
      dd.querySelectorAll(".chips button").forEach((b) => b.addEventListener("mousedown", (e) => {
        e.preventDefault(); input.value = b.dataset.q; query();
      }));
    } catch (err) { box.style.display = "none"; }
  }

  async function query() {
    const q = input.value.trim();
    if (q.length < 2) return showSuggestions();
    try {
      const r = await api(`/api/search?${new URLSearchParams({ q, limit: 8 })}`);
      if (input.value.trim() !== q) return;
      engine = r.engine; items = r.results; sel = -1;
      open(items.length ? items.map((it, i) => `<div class="it" data-i="${i}" role="option">
          <span class="k ${it.kind}">${ICON[it.kind] || "•"}</span><span><b>${esc(it.title)}</b><small>${esc(it.subtitle || "")}</small></span></div>`).join("")
        + `<div class="ft">${engLabel()} · ${r.ms} ms</div>`
        : `<div class="hd">Sin resultados</div><div class="ft">${engLabel()}</div>`);
      dd.querySelectorAll(".it").forEach((el) => el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(items[+el.dataset.i]); }));
    } catch (err) { open(`<div class="hd">${esc(err.message)}</div>`); }
  }

  function pick(it) {
    close(); input.blur();
    if (location.pathname === "/" || location.pathname === "") show(it.doc_id);
    else location.href = `/?buscar=${encodeURIComponent(it.doc_id)}`;
  }

  input.addEventListener("focus", () => (input.value.trim().length >= 2 ? query() : showSuggestions()));
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(query, 180); });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (e) => {
    const els = [...dd.querySelectorAll(".it")];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); if (!els.length) return;
      sel = (sel + (e.key === "ArrowDown" ? 1 : els.length - 1)) % els.length;
      els.forEach((el, i) => el.classList.toggle("sel", i === sel));
    } else if (e.key === "Enter" && items.length) { e.preventDefault(); pick(items[Math.max(0, sel)]); }
    else if (e.key === "Escape") { close(); input.blur(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") { e.preventDefault(); input.focus(); }
  });

  // ------------------------------------------------------------ result on the map (first tab)
  let layer = null, card = null;
  const theMap = () => (window.__navidadMaps || [])[0];
  function clearResult() { if (layer) layer.remove(); layer = null; if (card) card.remove(); card = null; }

  async function show(docId) {
    clearResult();
    let d;
    try { d = await api(`/api/search/doc/${encodeURIComponent(docId)}`); } catch (err) { alert(err.message); return; }
    const map = theMap();
    card = document.createElement("div");
    card.className = "ncard";
    const color = (BR[d.bringer] || [0, "#2a78d6"])[1];
    if (d.kind === "familia") {
      const kids = d.people.filter((p) => p.role === "child");
      const rows = d.history.map((h) => `<tr><td>${h.campaign === "reyes" ? `Reyes ${h.year + 1}` : `Nochebuena ${h.year}`}</td>
          <td>${(BR[h.bringer] || [h.bringer])[0]}</td><td>${tf(h.arrived_at_utc, h.timezone)}</td>
          <td>${h.crew ? esc(h.crew) : "—"}</td><td>${h.is_revisit ? "revisita" : ""}</td></tr>`).join("");
      card.innerHTML = `<header><div><b>${esc(d.title)}</b><small>${esc(d.subtitle)}</small></div><button aria-label="Cerrar">✕</button></header>
        <section><h4>Quién vive aquí</h4>${d.people.map((p) => `<span class="pill">${esc(p.given_name)} · ${p.age_years}${p.role === "child" && p.gift_affinity ? ` · ${esc(p.gift_affinity)}` : ""}</span>`).join("")}
          ${kids.length ? `<div style="color:#6b7280;font-size:.78rem;margin-top:.2rem">Regalos en «${esc(kids[0].window_policy || "—")}»</div>` : ""}
          ${d.live ? `<div class="live">Esta noche (${esc(d.live.sim_run_id)}): <b>${d.live.status === "entregado" ? "entregado" : "excepción"}</b> por ${esc(d.live.helper_id)} · ${tf(d.live.event_ts)}</div>` : ""}</section>
        <section><h4>Histórico de visitas (${d.history.length})</h4><table><tr><th>Noche</th><th>Portador</th><th>Llegada</th><th>Equipo</th><th></th></tr>${rows}</table></section>
        <div class="eng">${engLabel()} · datos: synced tables (*_sync)</div>`;
      if (map && d.lat != null) {
        layer = L.layerGroup([
          L.circleMarker([d.lat, d.lon], { radius: 14, color: "#ff3621", weight: 3, fillOpacity: 0 }),
          L.circleMarker([d.lat, d.lon], { radius: 6, color: "#fff", weight: 2, fillColor: "#ff3621", fillOpacity: 1 }).bindTooltip(esc(d.title)),
        ]).addTo(map);
        map.setView([d.lat, d.lon], 15);
      }
    } else {
      const who = d.kind === "paje" ? "Paje" : "Duende";
      card.innerHTML = `<header><div><b>${esc(d.title)}</b><small>${esc(d.subtitle)}</small></div><button aria-label="Cerrar">✕</button></header>
        <section><h4>Recorrido</h4><table>
          <tr><td>Paradas</td><td><b>${nf.format(d.stops)}</b></td></tr>
          <tr><td>Primera llegada</td><td>${tf(d.first_utc, d.timezone)}</td></tr>
          <tr><td>Última llegada</td><td>${tf(d.last_utc, d.timezone)}</td></tr>
          <tr><td>Equipo</td><td>${who} de ${(BR[d.bringer] || [d.bringer])[0]} · ruta ${d.route_id}</td></tr></table>
          <div style="color:#6b7280;font-size:.78rem;margin-top:.35rem">Línea = orden de las paradas (${nf.format(d.path.length)} puntos dibujados). Verde: inicio · rojo: final.</div></section>
        <div class="eng">${engLabel()} · datos: synced tables (*_sync)</div>`;
      if (map && d.path.length) {
        const line = L.polyline(d.path, { color, weight: 2.5, opacity: 0.85 });
        layer = L.layerGroup([line,
          L.circleMarker(d.path[0], { radius: 7, color: "#fff", weight: 2, fillColor: "#1f8f5f", fillOpacity: 1 }).bindTooltip("inicio"),
          L.circleMarker(d.path[d.path.length - 1], { radius: 7, color: "#fff", weight: 2, fillColor: "#d92d20", fillOpacity: 1 }).bindTooltip("final"),
        ]).addTo(map);
        map.fitBounds(line.getBounds(), { padding: [40, 40] });
      }
    }
    document.body.appendChild(card);
    card.querySelector("header button").addEventListener("click", clearResult);
    if (!map) card.querySelector(".eng").textContent += " · abre «Mapa de rutas» para verlo en el mapa";
  }

  // Arrived from another page with ?buscar=…: wait for the page to draw its own map first
  const want = new URLSearchParams(location.search).get("buscar");
  if (want) {
    let tries = 0;
    const go = () => (theMap() || tries++ > 20 ? setTimeout(() => show(want), 1200) : setTimeout(go, 250));
    go();
  }
})();

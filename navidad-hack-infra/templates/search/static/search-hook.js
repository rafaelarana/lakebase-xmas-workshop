// Include right after leaflet.js: remembers every Leaflet map the page creates, so search.js can draw on it.
(() => {
  if (!window.L || L.__navidadHooked) return;
  const orig = L.map;
  window.__navidadMaps = [];
  L.map = function (...args) { const m = orig.apply(this, args); window.__navidadMaps.push(m); return m; };
  L.__navidadHooked = true;
})();

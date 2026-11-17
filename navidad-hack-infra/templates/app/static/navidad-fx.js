// Navidad header effects (vanilla JS, no framework). Mounts into the first
// element with [data-navidad-header]: Santa hat on [data-navidad-logo], a tree +
// snowman scene on the right half, contained snowfall, and sky events (Santa's
// sleigh with reindeer, shooting stars). Decorative only: aria-hidden layers,
// pointer-events none. Off via <html data-navidad-fx="off">, the toggle button
// (persisted in localStorage "navidad-fx"), or prefers-reduced-motion (motion
// layers only). Add ?fx=rapido to the URL to see Santa within ~2 s (demo mode).
(() => {
  const STORAGE_KEY = "navidad-fx";
  const NS = "http://www.w3.org/2000/svg";
  const reduceMotion = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false, addEventListener() {} };
  const fast = new URLSearchParams(location.search).get("fx") === "rapido";
  // [first delay, mean interval, jitter] in ms
  const TIMING = fast
    ? { santa: [1500, 20000, 3000], star: [4000, 10000, 3000] }
    : { santa: [8000, 240000, 60000], star: [20000, 60000, 20000] };

  const svg = (attrs, inner) =>
    `<svg xmlns="${NS}" aria-hidden="true" ${attrs}>${inner}</svg>`;

  // --- Static art ---------------------------------------------------------
  const HAT = svg(
    'class="navidad-hat" viewBox="222 70 180 136"',
    '<g transform="rotate(14 300 182)">' +
      '<path d="M246 176 C252 120 300 84 362 92 C352 104 344 124 352 172 Z" fill="var(--color-holiday-red)"/>' +
      '<circle cx="366" cy="94" r="17" fill="var(--color-holiday-fur)" stroke="var(--color-holiday-fur-edge)" stroke-width="1" vector-effect="non-scaling-stroke"/>' +
      '<rect x="234" y="162" width="132" height="32" rx="16" fill="var(--color-holiday-fur)" stroke="var(--color-holiday-fur-edge)" stroke-width="1" vector-effect="non-scaling-stroke"/>' +
      "</g>"
  );

  function tier(apexY, half, baseY, fill) {
    const l = 20 - half;
    const r = 20 + half;
    return (
      `<path d="M20 ${apexY} L${r} ${baseY} H${l} Z" fill="${fill}"/>` +
      `<path d="M${l} ${baseY} Q${(l + 20) / 2} ${baseY - 4.2} 20 ${baseY - 1.8} Q${(r + 20) / 2} ${baseY - 4.8} ${r} ${baseY} Q20 ${baseY + 1.2} ${l} ${baseY} Z" fill="var(--color-holiday-fur)" opacity="0.9"/>`
    );
  }

  function starPoints(cx, cy, r) {
    return Array.from({ length: 10 }, (_, i) => {
      const rr = i % 2 === 0 ? r : r * 0.42;
      const a = -Math.PI / 2 + (i * Math.PI) / 5;
      return `${cx + rr * Math.cos(a)},${cy + rr * Math.sin(a)}`;
    }).join(" ");
  }

  function tree(height, depth, star) {
    const fill = depth === "front" ? "var(--color-holiday-pine)" : "var(--color-holiday-pine-back)";
    return svg(
      `viewBox="0 -14 40 74" height="${height}"`,
      "<g>" +
        '<rect x="17" y="52.8" width="6" height="7.2" fill="var(--color-holiday-trunk)"/>' +
        tier(0, 11.52, 27.6, fill) +
        tier(14.4, 15.36, 42, fill) +
        tier(28.8, 19.2, 56.4, fill) +
        '<path d="M20 0 L26 9.6 Q20 12 14 9.6 Z" fill="var(--color-holiday-tree-snow)" stroke="var(--color-holiday-tree-snow-edge)" stroke-width="1" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' +
        "</g>" +
        (star ? `<polygon points="${starPoints(20, -3, 7)}" fill="var(--color-holiday-gold)"/>` : "")
    );
  }

  function snowman(height) {
    const coal = "var(--color-holiday-coal)";
    const ball = (cx, cy, r) =>
      `<circle cx="${cx}" cy="${cy}" r="${r}" fill="var(--color-holiday-fur-shade)"/>` +
      `<circle cx="${cx - r * 0.1}" cy="${cy - r * 0.1}" r="${r * 0.9}" fill="var(--color-holiday-fur)"/>`;
    const dot = (cx, cy, r, fill = coal) => `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}"/>`;
    return svg(
      `viewBox="0 0 80 124" height="${height}"`,
      ball(40, 98, 24) +
        ball(40, 61, 17) +
        ball(40, 33, 12.5) +
        '<path d="M25 59 L3 45 M10 50 L7 40" stroke="var(--color-holiday-trunk)" stroke-width="2.5" stroke-linecap="round"/>' +
        '<path d="M55 59 L77 49 M71 52 L77 59" stroke="var(--color-holiday-trunk)" stroke-width="2.5" stroke-linecap="round"/>' +
        '<rect x="26" y="42" width="28" height="6.5" rx="3.2" fill="var(--color-holiday-red)"/>' +
        '<rect x="45" y="45" width="7" height="18" rx="3" fill="var(--color-holiday-red)"/>' +
        '<rect x="45" y="54" width="7" height="2.5" fill="var(--color-holiday-fur)" opacity="0.85"/>' +
        '<rect x="45" y="59" width="7" height="2.5" fill="var(--color-holiday-fur)" opacity="0.85"/>' +
        `<rect x="25" y="20" width="30" height="4" rx="1.5" fill="${coal}"/>` +
        `<rect x="30.5" y="2" width="19" height="19" rx="1.5" fill="${coal}"/>` +
        '<rect x="30.5" y="16" width="19" height="3.5" fill="var(--color-holiday-red)"/>' +
        dot(35.8, 30, 1.7) +
        dot(44.2, 30, 1.7) +
        dot(32.5, 35, 2.4, "var(--color-holiday-cheek)") +
        dot(47.5, 35, 2.4, "var(--color-holiday-cheek)") +
        '<path d="M40 33 L53 35 L40 36.5 Z" fill="var(--color-holiday-carrot)"/>' +
        [38, 39.5, 39.5, 38].map((cy, i) => dot(35.5 + i * 3, cy, 0.9)).join("") +
        dot(40, 57, 1.9) +
        dot(40, 66, 1.9) +
        dot(40, 92, 2.1)
    );
  }

  const SCENE_PROPS = [
    [6, () => tree(20, "back")],
    [10, () => tree(28, "front")],
    [26, () => tree(18, "back")],
    [30, () => tree(30, "front", true)],
    [34, () => tree(22, "back")],
    [50, () => snowman(28)],
    [68, () => tree(24, "back")],
    [72, () => tree(30, "front")],
    [89, () => tree(26, "front")],
    [93, () => tree(18, "back")],
  ];

  function scene() {
    return (
      `<div class="navidad-scene" aria-hidden="true">` +
      svg(
        'class="hill" viewBox="0 0 100 10" preserveAspectRatio="none"',
        '<path d="M0 10 C6 6 14 3 24 4 C34 5 40 7 50 5 C60 3 68 4 78 6 C88 8 94 4 100 6 V10 Z" fill="var(--color-holiday-mini-hill)"/>'
      ) +
      SCENE_PROPS.map(([at, el]) => `<div class="prop" style="left:${at}%">${el()}</div>`).join("") +
      "</div>"
    );
  }

  // --- Santa's sleigh -----------------------------------------------------
  function reindeer(cx, cy, lead) {
    const body = "var(--color-holiday-reindeer)";
    const leg = `stroke="${body}" stroke-width="2.4" stroke-linecap="round"`;
    return (
      "<g>" +
      `<path d="M${cx + 7} ${cy + 3} L${cx + 14} ${cy + 8} M${cx + 4} ${cy + 4} L${cx + 9} ${cy + 10}" ${leg}/>` +
      `<path d="M${cx - 7} ${cy + 3} L${cx - 15} ${cy + 6} M${cx - 4} ${cy + 4} L${cx - 10} ${cy + 9}" ${leg}/>` +
      `<ellipse cx="${cx}" cy="${cy}" rx="11" ry="5.2" fill="${body}"/>` +
      `<path d="M${cx + 7} ${cy - 2} L${cx + 13} ${cy - 9}" stroke="${body}" stroke-width="4.2" stroke-linecap="round"/>` +
      `<ellipse cx="${cx + 15.5}" cy="${cy - 10}" rx="4.4" ry="3.2" fill="${body}"/>` +
      `<path d="M${cx + 13} ${cy - 12} L${cx + 11} ${cy - 18} M${cx + 12} ${cy - 15} L${cx + 8} ${cy - 17} M${cx + 15} ${cy - 12} L${cx + 16} ${cy - 18} M${cx + 16} ${cy - 15} L${cx + 19} ${cy - 17}" stroke="var(--color-holiday-trunk)" stroke-width="1.4" stroke-linecap="round"/>` +
      `<circle cx="${cx - 11}" cy="${cy - 2.5}" r="2" fill="var(--color-holiday-fur)"/>` +
      `<circle cx="${cx + 19.6}" cy="${cy - 9.6}" r="${lead ? 2.3 : 1.3}" fill="${lead ? "var(--color-holiday-red)" : "var(--color-holiday-coal)"}"/>` +
      `<rect x="${cx - 3}" y="${cy - 5}" width="3" height="10" rx="1.2" fill="var(--color-holiday-red)"/>` +
      "</g>"
    );
  }

  // [x, y, size, isSparkle, delaySeconds]
  const TRAIL = [
    [-6, 30, 9, true, 0], [-18, 22, 2.6, false, 0.15], [-30, 34, 7, true, 0.3],
    [-42, 25, 2.2, false, 0.1], [-55, 19, 6, true, 0.45], [-67, 32, 2, false, 0.25],
    [-80, 25, 5, true, 0.05], [-92, 35, 1.6, false, 0.35], [-104, 21, 4, true, 0.2],
    [-116, 29, 1.4, false, 0.4],
  ];

  function sleigh() {
    const h = 22, w = 176, vh = 52;
    const trail = TRAIL.map(([x, y, s, sparkle, delay], i) => {
      const shape = sparkle
        ? `<path d="M${x} ${y - s} Q${x} ${y} ${x + s} ${y} Q${x} ${y} ${x} ${y + s} Q${x} ${y} ${x - s} ${y} Q${x} ${y} ${x} ${y - s} Z" fill="${i % 3 === 0 ? "var(--color-holiday-sparkle)" : "var(--color-holiday-gold)"}" opacity="${1 - i * 0.07}"/>`
        : `<circle cx="${x}" cy="${y}" r="${s}" fill="var(--color-holiday-gold)" opacity="${0.9 - i * 0.07}"/>`;
      return `<g class="sleigh-sparkle" style="animation-delay:${delay}s">${shape}</g>`;
    }).join("");
    return svg(
      `viewBox="0 -8 ${w} ${vh}" height="${h}" width="${(h * w) / vh}"`,
      trail +
        '<path d="M40 14 Q70 24 96 22 T158 13" fill="none" stroke="var(--color-holiday-gold)" stroke-width="1.2"/>' +
        reindeer(82, 24) +
        reindeer(116, 21) +
        reindeer(150, 18, true) +
        // sack
        '<circle cx="13" cy="14" r="9" fill="var(--color-holiday-trunk)"/>' +
        '<path d="M8 7 Q13 4 18 7" stroke="var(--color-holiday-gold)" stroke-width="1.6" fill="none"/>' +
        // Santa
        '<circle cx="33" cy="16" r="8.5" fill="var(--color-holiday-red)"/>' +
        '<circle cx="38" cy="6" r="4.6" fill="var(--color-holiday-skin)"/>' +
        '<ellipse cx="39" cy="10.5" rx="4.4" ry="3.6" fill="var(--color-holiday-fur)"/>' +
        '<path d="M33.5 4 L42.5 3 L37 -4.5 Z" fill="var(--color-holiday-red)"/>' +
        '<circle cx="36.6" cy="-5" r="1.8" fill="var(--color-holiday-fur)"/>' +
        '<rect x="33" y="2.6" width="10" height="2.4" rx="1.2" fill="var(--color-holiday-fur)"/>' +
        // sleigh body + runner
        '<path d="M4 34 L8 19 Q10 15 16 17 L46 21 Q53 22 55 27 L57 34 Z" fill="var(--color-holiday-red)"/>' +
        '<path d="M8 26 H54" stroke="var(--color-holiday-gold)" stroke-width="1.4"/>' +
        '<path d="M2 37 Q3 42 11 42 H51 Q59 42 60 35 Q61 30 56 30" fill="none" stroke="var(--color-holiday-gold)" stroke-width="2.6" stroke-linecap="round"/>' +
        '<path d="M18 34 V41 M44 34 V41" stroke="var(--color-holiday-gold)" stroke-width="2"/>'
    );
  }

  // --- Snowfall (seeded, so every load looks the same) ---------------------
  function mulberry32(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function snowfall(count) {
    const rand = mulberry32(1225);
    let html = "";
    for (let i = 0; i < count; i++) {
      const r = rand();
      const left = rand() * 100;
      const size = (2 + r * 5) * 0.7;
      const opacity = 0.35 + r * 0.55;
      const fall = (14 - r * 7) / 3;
      const sway = 3 + rand() * 3;
      const delay = -rand() * 14;
      html +=
        `<span class="snowflake-contained" style="left:${left.toFixed(2)}%;animation-duration:${fall.toFixed(2)}s;animation-delay:${(delay / 3).toFixed(2)}s">` +
        `<span class="snowflake-sway" style="width:${size.toFixed(2)}px;height:${size.toFixed(2)}px;opacity:${opacity.toFixed(2)};animation-duration:${sway.toFixed(2)}s;animation-delay:${delay.toFixed(2)}s"></span>` +
        "</span>";
    }
    return `<div class="snowfall" aria-hidden="true">${html}</div>`;
  }

  // --- Sky events scheduler ----------------------------------------------
  function startSky(sky) {
    const timers = new Set();
    const spawn = (kind) => {
      if (document.hidden || reduceMotion.matches) return;
      const el = document.createElement(kind === "santa" ? "div" : "span");
      if (kind === "santa") {
        el.className = "santa-flight";
        el.innerHTML = `<div class="santa-bob">${sleigh()}</div>`;
      } else {
        el.className = "shooting-star";
        el.style.right = `${8 + Math.random() * 36}%`;
      }
      el.addEventListener("animationend", (e) => {
        if (e.target === el) el.remove();
      });
      sky.appendChild(el);
    };
    const schedule = (kind, delay) => {
      const id = window.setTimeout(() => {
        timers.delete(id);
        spawn(kind);
        const [, mean, jitter] = TIMING[kind];
        schedule(kind, mean + (Math.random() * 2 - 1) * jitter);
      }, delay);
      timers.add(id);
    };
    schedule("santa", TIMING.santa[0]);
    schedule("star", TIMING.star[0]);
    return () => {
      timers.forEach((id) => window.clearTimeout(id));
      timers.clear();
      sky.replaceChildren();
    };
  }

  // --- Mount ----------------------------------------------------------------
  // The visitor's toggle (localStorage) wins; <html data-navidad-fx> is the default.
  function isOn() {
    let stored = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      /* storage blocked */
    }
    if (stored === "on" || stored === "off") return stored === "on";
    return document.documentElement.dataset.navidadFx !== "off";
  }

  function mount(header) {
    const logo = header.querySelector("[data-navidad-logo]");
    const layer = document.createElement("div");
    layer.className = "navidad-fx-layer";
    layer.setAttribute("aria-hidden", "true");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "navidad-fx-toggle";
    header.appendChild(toggle);
    header.prepend(layer);

    let stopSky = null;
    let hat = null;

    const render = () => {
      const on = isOn();
      toggle.textContent = on ? "❄ Efectos: sí" : "❄ Efectos: no";
      toggle.setAttribute("aria-pressed", String(on));
      toggle.title = on ? "Desactivar efectos navideños" : "Activar efectos navideños";
      if (stopSky) stopSky();
      stopSky = null;
      layer.replaceChildren();
      if (hat) hat.remove();
      hat = null;
      if (!on) return;
      layer.innerHTML = snowfall(18) + scene() + '<div class="sky-events" aria-hidden="true"></div>';
      stopSky = startSky(layer.querySelector(".sky-events"));
      if (logo) {
        logo.insertAdjacentHTML("beforeend", HAT);
        hat = logo.lastElementChild;
      }
    };

    toggle.addEventListener("click", () => {
      const next = isOn() ? "off" : "on";
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        /* storage blocked: toggle only for this page view */
      }
      document.documentElement.dataset.navidadFx = next;
      render();
    });
    render();
  }

  function init() {
    const header = document.querySelector("[data-navidad-header]");
    if (header && !header.dataset.navidadFxMounted) {
      header.dataset.navidadFxMounted = "1";
      mount(header);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

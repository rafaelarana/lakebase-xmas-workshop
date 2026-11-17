// Left navigation rail: include right after <body> with <script src="/static/nav.js"></script>
// (and /static/nav.css in <head>). Builds the rail synchronously so maps size correctly, highlights the
// current page and removes any leftover page links from the header.
(() => {
  const PAGES = [
    { href: "/", label: "Mapa de rutas", icon: '<path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z"/><path d="M9 4v14M15 6v14"/>' },
    { href: "/control", label: "Centro de control", icon: '<circle cx="12" cy="12" r="8"/><path d="M12 12l4-3"/><path d="M12 4v2M20 12h-2M12 20v-2M4 12h2"/>' },
    { href: "/planner", label: "Planificador", alias: ["/incidencias"], icon: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>' },
  ];
  const MINOR = [{ href: "/plumbing", label: "Diagnóstico", icon: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>' }];
  const here0 = location.pathname.replace(/\/+$/, "") || "/";
  const here = (PAGES.find((p) => (p.alias || []).includes(here0)) || {}).href || here0;
  const link = (p, cls = "") =>
    `<a href="${p.href}" class="${cls}" title="${p.label}"${here === p.href ? ' aria-current="page"' : ""}>` +
    `<svg viewBox="0 0 24 24" aria-hidden="true">${p.icon}</svg><span>${p.label}</span></a>`;
  const nav = document.createElement("nav");
  nav.className = "app-rail";
  nav.setAttribute("aria-label", "Secciones del app");
  nav.innerHTML = `<div class="brand"><img src="/static/databricks-symbol.svg" alt="Databricks" /></div>`
    + PAGES.map((p) => link(p)).join("") + `<div class="spacer"></div>` + MINOR.map((p) => link(p, "minor")).join("");
  (document.body || document.documentElement).prepend(nav);
  document.documentElement.classList.add("has-rail");

  const tidy = () => {  // pages built before the rail still carry header links
    const hrefs = new Set([...PAGES, ...MINOR].flatMap((p) => [p.href, ...(p.alias || [])]));
    document.querySelectorAll("header a[href]").forEach((a) => { if (hrefs.has(a.getAttribute("href"))) a.remove(); });
    document.querySelectorAll("header .links").forEach((s) => { if (!s.textContent.trim()) s.remove(); });
    window.dispatchEvent(new Event("resize"));  // let Leaflet maps pick up the new width
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tidy); else tidy();
})();

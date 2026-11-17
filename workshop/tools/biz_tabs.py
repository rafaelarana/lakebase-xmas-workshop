"""Insert / refresh the «Valor de negocio» first subtab in every section of the workshop deck.

Single source for the business texts. Idempotent: re-running replaces what it inserted
(markers <!-- BIZ … --> in the HTML). Rules: never name competitors; only figures measured
in the workshop (reference workspace) or taken from the docs.

    python3 workshop/tools/biz_tabs.py            # rewrites workshop/deck/lakebase-agentes-es.html (ES only; the EN deck was translated from its output — edit lakebase-agents-en.html by hand)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DECK = Path(__file__).resolve().parents[1] / "deck" / "lakebase-agentes-es.html"

# section id → card texts. Inline HTML allowed (<code>, <b>).
BIZ: dict[str, dict[str, str]] = {
    "b0": {
        "pain": "Cada app o agente necesita su base operacional, y se monta aparte: otro equipo, otra seguridad, otra copia de los datos.",
        "lb": "La base operacional vive dentro de la plataforma de datos e IA, junto al lakehouse, los modelos y las apps.",
        "val": "De la idea a un app con datos reales en una mañana, con un solo workspace.",
        "why": "Una sola plataforma para datos, base operacional, apps y modelos: un modelo de permisos, un sitio donde auditar.",
        "proof": "Taller completo desde cero: ≈ 5 min a escala 0,1 · 15 min a escala 1,0 (500.000 hogares)",
        "q": "¿Cuántas apps internas mantienen hoy su propia base de datos y su propia copia de los datos?",
        "note": "Hoy no venimos a hablar de una base de datos más, sino de dónde viven las apps y los agentes: al lado de los datos.",
    },
    "b1": {
        "pain": "Operar Postgres es dimensionar, parchear, gestionar credenciales y pagar el servidor 24/7, se use o no.",
        "lb": "Postgres serverless gestionado: cómputo separado del almacenamiento, ramas, autoscaling y scale-to-zero.",
        "val": "Menos operación y un coste que sigue al uso real, no al pico.",
        "why": "Postgres estándar, sin dialectos propios ni dependencia de un proveedor, e integrado con el lakehouse: no es otra base que integrar.",
        "proof": "Se reactiva en unos cientos de ms tras scale-to-zero · autoscaling de 0,5 a 64 CU (documentación)",
        "q": "¿Cuánto esfuerzo de operación dedicáis hoy a vuestras bases transaccionales?",
        "note": "Cinco ideas, un mensaje: Postgres de verdad, sin servidor que cuidar, pegado a vuestros datos.",
    },
    "b2": {
        "pain": "Llevar datos analíticos a un app suele ser ETL a mano, credenciales repartidas y semanas de proyecto.",
        "lb": "Synced tables desde Unity Catalog y un app con identidad propia (OAuth): sin pipelines a medida ni contraseñas.",
        "val": "Time-to-value en horas y menos piezas que puedan fallar.",
        "why": "Los datos no salen de la plataforma: mismo linaje, mismos permisos, misma auditoría.",
        "proof": "4 synced tables (≈ 4,5 M filas) en línea en 30 s · app desplegada en ≈ 3 min",
        "q": "¿Cuántos pipelines mantenéis solo para copiar datos del lakehouse a vuestras apps?",
        "note": "Mientras corre el Run all, lo importante no es la técnica: es que no hay ETL que mantener.",
    },
    "rt": {
        "pain": "Los paneles operativos enseñan datos de hace horas, y el tiempo real exige encadenar colas, bases y cachés.",
        "lb": "Eventos a Lakebase en segundos con ingesta gestionada, o escritura directa en milisegundos; el agente decide sobre datos frescos.",
        "val": "Reaccionar durante la noche, no a la mañana siguiente.",
        "why": "El histórico (Delta) y el estado vivo (Postgres) en la misma plataforma: sin sincronizar dos mundos.",
        "proof": "Escritura directa p50 ≈ 20 ms · Zerobus → Spark → Lakebase p50 12 s / p95 18 s · revisión del agente 7–10 s",
        "q": "¿Qué decisiones tomaríais si el dato operativo llegara en segundos y no en horas?",
        "note": "El valor no es la latencia en sí: es poder actuar mientras pasa, con un agente que propone y una persona que confirma.",
    },
    "replica": {
        "pain": "Se dimensiona para el pico y se paga todo el año; y las lecturas del panel compiten con las escrituras.",
        "lb": "Autoscaling, scale-to-zero y réplicas de lectura en segundos, sobre el mismo almacenamiento, sin copiar datos.",
        "val": "Aguantar el pico (¡Nochebuena!) sin rediseñar y pagar por uso el resto del año.",
        "why": "Escalar o añadir una réplica es una operación de segundos, no un proyecto de capacidad.",
        "proof": "Réplica de lectura creada en 2 s · retraso p50 3 ms",
        "q": "¿Cuál es vuestro pico de demanda y cuántos días al año lo pagáis sin usarlo?",
        "note": "Nochebuena es el pico perfecto: un día al año. Nadie quiere pagar 365 días por él.",
    },
    "b3": {
        "pain": "Probar un cambio o un «qué pasa si» obliga a copiar la base (horas, coste, datos viejos) o a tocar producción.",
        "lb": "Ramas copy-on-write en segundos, con su propio cómputo, que caducan solas.",
        "val": "Decidir con datos reales y sin riesgo: planes de crisis, escenarios y pruebas.",
        "why": "La rama es la unidad de trabajo: una por simulación, por agente o por desarrollador, y se tira al acabar.",
        "proof": "Rama en 2–3 s · DANA: 1.014 hogares sin acceso y plan alternativo en ≈ 6–8 s · producción: 0 cambios",
        "q": "¿Cómo probáis hoy un escenario o una migración sin tocar producción?",
        "note": "Escenarios inspirados en hechos reales con víctimas: tono serio. El mensaje es planificar mejor, no el chiste.",
    },
    "b4": {
        "pain": "Un agente que escribe en producción es un riesgo; uno al que no dejas actuar aporta poco.",
        "lb": "El agente trabaja en una rama, prueba y pide aprobación; sobre Postgres estándar, que los modelos ya conocen.",
        "val": "Desarrollo mucho más rápido sin perder el control humano.",
        "why": "Gobierno incorporado: permisos mínimos, identidad del app y aprobación explícita antes de producción.",
        "proof": "Tabla nueva + endpoint + pruebas + migración a producción en ≈ 8 min (grabación real)",
        "q": "¿Qué tareas dejaríais a un agente si pudiera equivocarse sin consecuencias?",
        "note": "La pregunta del cliente no es si el agente acierta, sino qué pasa cuando se equivoca. La rama es la respuesta.",
    },
    "grinch": {
        "pain": "Un error humano o un script borra datos operacionales: horas de restauración y el servicio caído.",
        "lb": "Recuperación a un instante (PITR) y snapshots: rama desde el momento seguro y vuelta atrás sin parar el app.",
        "val": "Recuperar en segundos, sin pérdida y sin parada.",
        "why": "La recuperación es una rama: se inspecciona antes de restaurar, en lugar de restaurar a ciegas.",
        "proof": "Rama al momento seguro en 2,4 s · 372.830 cartas recuperadas en 5 s · app sin parar",
        "q": "¿Cuánto tardaríais hoy en recuperar una tabla borrada por error, y qué se pararía mientras tanto?",
        "note": "Todos tenemos un Grinch: un DELETE sin WHERE. Lo que cambia es cuánto cuesta deshacerlo.",
    },
    "busqueda": {
        "pain": "Un buscador suele ser otro motor, otro índice y otra sincronización que mantener al día.",
        "lb": "Búsqueda BM25, vectorial e híbrida dentro de Postgres, también en la réplica de lectura.",
        "val": "Menos sistemas y resultados siempre al día con los datos.",
        "why": "La búsqueda vive donde viven los datos y sus permisos: una sola fuente de verdad.",
        "proof": "500.000 familias indexadas en 25 s · consulta BM25 ≈ 3 ms",
        "q": "¿Cuántos sistemas tocáis hoy para que un buscador muestre datos actualizados?",
        "note": "Para agentes, buscar es memoria: tenerla en la misma base que el estado simplifica todo.",
    },
}

# Cierre: summary rows (section label, pain, value, figure), revealed one by one
SUMMARY = [
    ("Plataforma", "Una base aparte por app", "Datos, apps y modelos en un workspace", "Taller completo ≈ 5 min"),
    ("1 · Ideas", "Operar y pagar servidores 24/7", "Postgres serverless, coste por uso", "Reactivación en cientos de ms"),
    ("2 · Desde cero", "ETL a mano, semanas", "Synced tables, horas", "4 tablas en línea en 30 s"),
    ("3 · Tiempo real", "Datos de hace horas", "Actuar mientras pasa", "Directo p50 ≈ 20 ms"),
    ("4 · Escala", "Pagar el pico todo el año", "Autoscaling y réplicas en segundos", "Réplica en 2 s, retraso 3 ms"),
    ("5 · Branching", "Copiar bases para probar", "Ramas en segundos, sin riesgo", "Rama en 2–3 s, producción intacta"),
    ("6 · Agéntico", "Agentes que tocan producción", "Rama + pruebas + aprobación", "Feature completa en ≈ 8 min"),
    ("7 · Grinch", "Horas de restauración", "Recuperar sin parar el app", "372.830 cartas en 5 s"),
    ("8 · Búsqueda", "Otro motor que sincronizar", "Búsqueda dentro de Postgres", "BM25 ≈ 3 ms"),
]

CSS = """
  /* BIZ-CSS · «Valor de negocio» tab (generated by workshop/tools/biz_tabs.py) */
  .biz { display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px 34px; counter-reset:none; margin-top:4px; }
  .biz li { padding:18px 20px 20px; border-radius:16px; min-height:140px; cursor:pointer; position:relative; transition:all .35s; }
  .biz li::before { display:none; }
  .biz li .bl { display:block; font:800 12px Inter,sans-serif; letter-spacing:.16em; text-transform:uppercase; margin-bottom:10px; }
  .biz li p { font-size:18px; line-height:1.45; color:var(--white); }
  .biz li.b-pain .bl { color:var(--muted); } .biz li.b-pain { border-color:var(--line); }
  .biz li.b-lb { border-color:var(--teal); } .biz li.b-lb .bl { color:var(--teal); }
  .biz li.b-val { border-color:var(--lava); } .biz li.b-val .bl { color:var(--lava-soft); }
  .biz li.b-pain:not(:last-child)::after, .biz li.b-lb::after { content:"→"; position:absolute; right:-28px; top:50%; transform:translateY(-50%);
    font:700 24px Inter,sans-serif; color:var(--muted); }
  .biz li.b-why { grid-column:1 / -1; min-height:0; display:grid; grid-template-columns:1.3fr 1fr 1fr; gap:18px; border-color:var(--line); }
  .biz li.b-why > div { font-size:15px; line-height:1.45; color:var(--white); }
  .biz li.b-why .bl { margin-bottom:6px; }
  .biz li.b-why .w .bl { color:var(--amber); } .biz li.b-why .m .bl { color:var(--green); } .biz li.b-why .q .bl { color:var(--teal); }
  .biz li.b-why .m b { color:var(--white); }
  .biz li.on { box-shadow:0 0 0 2px var(--lava); }
  .biz:has(li.on) li:not(.on):not(.done) { opacity:.22; }
  .biz-sum { display:grid; gap:6px; counter-reset:none; }
  .biz-sum li { display:grid; grid-template-columns:150px 1fr 1fr 1fr; gap:16px; padding:10px 14px; align-items:center; }
  .biz-sum li::before { display:none; }
  .biz-sum-h { display:grid; grid-template-columns:150px 1fr 1fr 1fr; gap:16px; padding:0 15px 6px; font:800 11.5px Inter,sans-serif; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); }
  .biz-sum li b { color:var(--white); } .biz-sum li .v { color:var(--lava-soft); font-weight:600; } .biz-sum li .f { color:var(--green); }
  .biz-sum:has(li.on) li:not(.on):not(.done) { opacity:.22; }
  .subtab.biz-tab small { color:var(--lava-soft); }
  /* /BIZ-CSS */
"""


def card_pane(d: dict[str, str]) -> str:
    return f"""<div class="pane biz-pane">
    <ol class="steps biz">
      <li data-step="1" class="b-pain"><span class="bl">El dolor de hoy</span><p>{d['pain']}</p></li>
      <li data-step="2" class="b-lb"><span class="bl">Con Lakebase</span><p>{d['lb']}</p></li>
      <li data-step="3" class="b-val"><span class="bl">El valor</span><p>{d['val']}</p></li>
      <li data-step="4" class="b-why">
        <div class="w"><span class="bl">Por qué es distinto</span>{d['why']}</div>
        <div class="m"><span class="bl">Medido en el taller</span><b>{d['proof']}</b></div>
        <div class="q"><span class="bl">Pregunta para el cliente</span>«{d['q']}»</div>
      </li>
    </ol>
  </div>"""


def summary_pane() -> str:
    rows = "\n".join(
        f'      <li data-step="{i}"><b>{a}</b><span>{p}</span><span class="v">{v}</span><span class="f">{f}</span></li>'
        for i, (a, p, v, f) in enumerate(SUMMARY, start=1))
    return f"""<div class="pane biz-pane">
    <div class="biz-sum-h"><span>Bloque</span><span>El dolor de hoy</span><span>Con Lakebase</span><span>Medido en el taller</span></div>
    <ol class="steps biz-sum">
{rows}
    </ol>
    <p style="margin-top:12px;color:var(--muted-2)"><b>Siguiente paso con el cliente:</b> repetir el «Run all» con sus datos y elegir el primer caso (app sobre datos del lakehouse, agente con estado o what-if en ramas).</p>
  </div>"""


TAB = '<button class="subtab biz-tab" data-pane="0">Valor de negocio <small>para el cliente</small></button>'


def apply(html: str) -> str:
    # 1) CSS (once, refreshed)
    html = re.sub(r"\n  /\* BIZ-CSS.*?/\* /BIZ-CSS \*/\n", "", html, flags=re.S)
    html = html.replace("</style>", CSS + "</style>", 1)

    def section(m: re.Match) -> str:
        sid, body = m.group(1), m.group(0)
        if sid not in BIZ and sid != "b5":
            return body
        # drop a previous insertion
        body = re.sub(r'<!-- BIZ-PANE -->.*?<!-- /BIZ-PANE -->\n  ', "", body, flags=re.S)
        body = re.sub(r'<button class="subtab biz-tab"[^>]*>.*?</button>', "", body, flags=re.S)
        body = re.sub(r'<li class="biz-note">.*?</li>', "", body, flags=re.S)
        # tabs: new first + renumber, only the business tab is active
        tabs = re.search(r'<div class="subtabs">(.*?)</div>', body, re.S)
        if tabs is None or not re.search(r'<div class="pane[ "]', body):
            raise SystemExit(f"section {sid}: needs <div class=\"subtabs\"> and at least one .pane to get a business tab")
        inner = re.sub(r'class="subtab active"', 'class="subtab"', tabs.group(1))
        btns = re.findall(r'<button class="subtab"[^>]*>.*?</button>', inner, re.S)
        btns = [re.sub(r'data-pane="\d+"', f'data-pane="{i + 1}"', b) for i, b in enumerate(btns)]
        body = body.replace(tabs.group(0), '<div class="subtabs">' + TAB.replace('class="subtab biz-tab"', 'class="subtab biz-tab active"') + "".join(btns) + "</div>", 1)
        # panes: the business pane goes first and is the active one
        body = re.sub(r'<div class="pane active( [^"]*)?">', lambda p: f'<div class="pane{p.group(1) or ""}">', body)
        first = re.search(r'\n(\s*)<div class="pane[ "]', body)
        pane = (summary_pane() if sid == "b5" else card_pane(BIZ[sid])).replace('class="pane biz-pane"', 'class="pane biz-pane active"', 1)
        body = body[:first.start()] + "\n  <!-- BIZ-PANE -->" + pane + "<!-- /BIZ-PANE -->\n  " + body[first.start():].lstrip("\n").lstrip()
        # speaker note
        if sid in BIZ:
            note = f'<li class="biz-note"><b>Negocio:</b> {BIZ[sid]["note"]}</li>'
            body = re.sub(r'(<div class="note-src"><h4>[^<]*</h4><ul>)', lambda m: m.group(1) + note, body, count=1)
        return body

    html = re.sub(r'<section class="block" id="(\w+)".*?</section>', section, html, flags=re.S)
    return html


def main() -> int:
    html = DECK.read_text(encoding="utf-8")
    out = apply(html)
    if apply(out) != out:
        print("not idempotent", file=sys.stderr)
        return 1
    DECK.write_text(out, encoding="utf-8")
    print(f"updated {DECK.name}: {len(BIZ)} sections + Cierre summary")
    return 0


if __name__ == "__main__":
    sys.exit(main())

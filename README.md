# Lakebase Xmas Workshop

A Spain-themed Lakebase hackathon workbench that starts at the interesting part: Santa Claus and the Three Kings (Reyes Magos) as a delivery fleet across 21 Spanish cities.

> **Not an official Databricks product.** A community workshop kit: synthetic data, provided as is.
> Licence: Apache-2.0 (see `LICENSE` and `NOTICE`; OpenStreetMap data stays ODbL).

**Just want to run the workshop?** Download `lakebase-xmas-workshop.zip` from the Releases page (or build it with
`workshop/build_zip.sh`), import it into your workspace (*Workspace → Import → File*) and follow
`workshop/README.md`. The deck is `workshop/deck/lakebase-agents-en.html` (opens offline).

| Skill | What it does |
| --- | --- |
| `navidad-hack-infra/` | Generates the synthetic Spain dataset, writes it to Unity Catalog, provisions a Lakebase Autoscaling project with inbound synced tables, deploys a Spanish-UI plumbing Databricks App |
| `navidad-hack-build-assistant/` | Genie playbooks for maps (PostGIS), routes, segmentation, incident simulations on Lakebase branches and app customisation on that workbench |
| `workshop/` | 45' partner workshop: notebooks (from zero, branching, Genie Code skills, Grinch/PITR, cleanup), shareable SQL, deck |

## Highlights

- **Data:** 21 Spanish cities (peninsula, Balearics, Canaries) weighted by population; Spanish surnames and given names (with Catalan/Basque/Galician/Balearic names in those regions); `postcode_area` = 2-digit province code; new `province`, `autonomous_community`, `timezone` columns.
- **Two nights per campaign year:** Nochebuena (Papá Noel, Olentzero, Tió de Nadal or Apalpador by region, `nochebuena_bringer`) and Reyes (Melchor, Gaspar, Baltasar, each owning a 120° sector of every city, `visits_reyes`, `favourite_king`).
- **Journeys:** one row per household visit, keyed by (`year`, `campaign`, `route_id`, `stop_number`), with the `bringer`. Routes are compressed to fit the night in local time; the Canaries run one hour behind.
- **No homes in the sea:** per-city land masks (`scripts/es_land.py`) cut from Natural Earth 10m land (public domain) by the dev-only `tools/build_land_masks.py`.
- **Categorical values** are Spanish ASCII codes (`juguetes`, `zapatos`, `frutos_secos`).
- **App UI in Spanish** (`lang="es"`, es-ES formatting, Spanish model ping); skills, code and SQL stay in English.
- **Maps:** OSM muted (default) or IGN España base map (keyless, CC BY 4.0); Península / Canarias toggle instead of one view.
- **Names:** `navidad_<slug>` / `navidad-<slug>` / `navidad-<slug>-app`, so it can sit next to other demo workbenches in the same workspace. CLI `--profile` is required.

## Tests

```bash
uv venv .venv --python 3.11 && uv pip install --python .venv/bin/python pytest fastapi httpx uvicorn psycopg2-binary databricks-sdk==0.122.0
(cd navidad-hack-infra && ../.venv/bin/python -m pytest -q)
(cd navidad-hack-build-assistant && ../.venv/bin/python -m pytest -q)
```

Regenerating land masks (only if city coordinates change) additionally needs `shapely` and `ne_10m_land.geojson`.

> Spanish edition: https://github.com/rafaelarana/lakebase-taller-navidad

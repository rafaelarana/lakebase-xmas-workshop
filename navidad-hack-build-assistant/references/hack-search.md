# Search («Búsqueda») — hack playbook (agent)

**Story.** A search bar on every page: type a family or a duende / paje crew and see it on the map — the household's location with its visit history across years and nights, or the path a crew followed.

## What exists (built by `navidad-hack-infra`)

- **Lakebase Search must be enabled on the project** (Lakebase → project → Settings → Lakebase Search). It is irreversible and restarts the project's computes. Without it, `CREATE EXTENSION lakebase_text` fails with «must be loaded via shared_preload_libraries» and the index falls back to Postgres full-text (GIN).
- Notebook `08_busqueda` (`provision_search.py`, `sql/search.sql`) builds `navidad_search.doc`: one document per family (label, city, members' first names) and per crew (route segment, same names and split as the control center: `live_sim.assign_helpers`, 4 per route). `search_vector = to_tsvector('simple', navidad_search.norm(text))` (lower + unaccent).
- Indexes: `lakebase_bm25` (`doc_bm25`, rebuilt **after** loading — BM25 corpus stats are computed at build time) + GIN (`doc_fts`) for typeahead prefixes.
- App (`templates/search`): `GET /api/search?q=` (BM25 ⊕ unranked prefix match, RRF), `GET /api/search/suggestions`, `GET /api/search/doc/{doc_id}` (family: people, history, crew, live status; crew: ordered path), `GET /api/search/status`. Reads through the control center's `ReadRouter` (read replica if any). `static/search.js` adds the box to every header (`/` shortcut); `search-hook.js` (right after leaflet.js) lets it draw on the page's map; results open on «Mapa de rutas» (`/?buscar=<doc_id>`).
- Measured (reference workspace): BM25 2.7 ms server-side; ranking every prefix match with `ts_rank` ~146 ms, unranked ~31 ms (so BM25 ranks); index build 3 s; 500,000 families loaded in 26–48 s.

## Rules

- Keep the analyser `simple` + `unaccent`. **Don't** add `lakebase_tokenizer` stop-word or synonym tables: they assign a transaction id on first use and fail on read replicas (the app reads from the replica).
- Query normalisation in Python must match `navidad_search.norm` (lower, strip accents incl. ñ → n).
- Rebuild the BM25 index after large reloads (`SELECT navidad_search.build_bm25()`).
- New searchable kinds go into `navidad_search.doc` with a `kind`, a title/subtitle and a location; the UI renders them generically.
- New objects follow the branch-first workflow (`dev-branch-workflow.md`).

## Extension ideas

- **Semantic search** (`lakebase_vector`): embed a short description per family («familia con dos niños pequeños, regalos en el balcón») and run hybrid BM25 + vector («familias con niños pequeños cerca del puerto»).
- Filters in the box: `ciudad:Sevilla`, `noche:reyes`, `tipo:paje`.
- Search exceptions of a night run (`navidad_live.exception_event`) by code and city.

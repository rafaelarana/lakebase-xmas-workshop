-- Bonus «Búsqueda» · Lakebase Search over families and duende/paje crews
-- Idempotent. __SCHEMA__ = workbench schema (navidad_<tag>). Built by provision_search.py.
--
-- One searchable document per family (households_sync + members' first names) and per helper crew
-- (route segment of a duende / paje, same names as the control center). Analyser: 'simple' on
-- lower(unaccent(text)) — no stop-word tables, so it also works on read replicas.
-- Index: lakebase_bm25 (Lakebase Search, BM25 ranking) when the extension is enabled on the project,
-- plus a GIN index for typeahead prefixes and as the fallback when Search is not enabled.

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE SCHEMA IF NOT EXISTS navidad_search;

CREATE OR REPLACE FUNCTION navidad_search.norm(t text) RETURNS text
LANGUAGE sql STABLE AS $$ SELECT lower(unaccent(coalesce(t, ''))) $$;

CREATE TABLE IF NOT EXISTS navidad_search.doc (
  doc_id        text PRIMARY KEY,           -- h:<household_id> | c:<campaign>:<year>:<route>-<segment>
  kind          text NOT NULL,              -- familia | duende | paje
  title         text NOT NULL,
  subtitle      text,
  city          text,
  household_id  bigint,
  campaign      text,
  year          int,
  route_id      bigint,
  stop_from     bigint,
  stop_to       bigint,
  bringer       text,
  lat           double precision,
  lon           double precision,
  body          text NOT NULL,
  search_vector tsvector NOT NULL
);
CREATE INDEX IF NOT EXISTS doc_kind ON navidad_search.doc (kind);
CREATE INDEX IF NOT EXISTS doc_fts ON navidad_search.doc USING gin (search_vector);

-- Families: «Familia Suárez Medina» + city + members' first names
CREATE OR REPLACE FUNCTION navidad_search.load_families() RETURNS int LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  DELETE FROM navidad_search.doc WHERE kind = 'familia';
  INSERT INTO navidad_search.doc (doc_id, kind, title, subtitle, city, household_id, lat, lon, body, search_vector)
  SELECT 'h:' || h.household_id, 'familia', h.label,
         h.city || ' · ' || h.household_size || CASE WHEN h.household_size = 1 THEN ' persona' ELSE ' personas' END ||
           CASE WHEN coalesce(p.children, 0) > 0 THEN ' · ' || p.children || CASE WHEN p.children = 1 THEN ' niño' ELSE ' niños' END ELSE '' END,
         h.city, h.household_id, h.lat, h.lon,
         concat_ws(' ', h.label, h.city, h.province, p.names, 'familia hogar'),
         to_tsvector('simple', navidad_search.norm(concat_ws(' ', h.label, h.city, h.province, p.names, 'familia hogar')))
  FROM __SCHEMA__.households_sync h
  LEFT JOIN (SELECT household_id, string_agg(given_name, ' ' ORDER BY person_id) AS names,
                    count(*) FILTER (WHERE role = 'child') AS children
             FROM __SCHEMA__.people_sync GROUP BY household_id) p USING (household_id);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- BM25 index: (re)built after loading, because BM25 corpus statistics are computed at build time
CREATE OR REPLACE FUNCTION navidad_search.build_bm25() RETURNS text LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'lakebase_text') THEN
    RETURN 'fts';      -- Lakebase Search not enabled: Postgres full-text (GIN) only
  END IF;
  DROP INDEX IF EXISTS navidad_search.doc_bm25;
  CREATE INDEX doc_bm25 ON navidad_search.doc USING lakebase_bm25 (search_vector) WITH (default_limit = 50);
  RETURN 'bm25';
END $$;

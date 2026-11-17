# Customer segmentation (agent)

Explore → Interpret → Build. Compute in **Lakebase** from `*_sync`. New table beside them. Do not k-means in FastAPI.

## Explore

Orient in chat (counts by city or comunidad, score bands, gift affinity, Nochebuena bringer). Then a simple **app** viz: e.g. bar of gift affinity, or map coloured by household mean naughty/nice. One year, one city for v1. Labels in Spanish.

**Suggest dimensions** (don't ask “what segments?”):

- Gift affinity
- Naughty/nice bands, framed as carbón (e.g. 0–20 carbón / 21–79 regular / 80–100 muy bueno)
- Tradition: Nochebuena bringer × Reyes (`nochebuena_bringer`, `visits_reyes`) — e.g. “Olentzero + Reyes” households in Bilbao
- Favourite King vs the King who actually serves the household's sector
- Household size or child/adult mix
- Combination of score band × gift affinity

## Interpret

“Which of those would change how the Kings pack the camels or time the stop?” Let them react.

## Build

Grain: preferences per **person**; geo, tradition and size per **household**; journeys per household-night. Join `people_sync` → `preferences_sync` on `person_id`, `people_sync` → `households_sync` on `household_id`. Aggregate journeys before joining to people (a household can have two visits per year).

Persist `{schema}.person_segments`. GRANT SELECT (`references/lakebase-sql.md`). Do not `UPDATE` `preferences_sync`. Do not write a new Unity Catalog table unless they explicitly ask.

App (check `references/app-endpoints.md` first):

- `GET /api/segments` — counts
- `GET /api/segment-members?label=&limit=50` — samples only

Optional Gateway paragraph in Spanish on **aggregates**, not raw dumps.

# Installing the Navidad hack skills (Genie Code)

Two skills, installed side by side:

- `navidad-hack-infra` — sets up the workbench (UC tables → Lakebase + synced tables → Spanish-UI app)
- `navidad-hack-build-assistant` — guides maps, routes, segments and app changes afterwards

## Requirements

- Databricks CLI (v1.x) authenticated to the target workspace (`databricks auth login --host <url> --profile <name>`)
- Workspace with serverless compute, Lakebase Autoscaling, Databricks Apps and the AI Gateway model `system.ai.claude-opus-5`
- A Unity Catalog catalog where participants can create schemas (the skills never create catalogs)

## Install

```bash
unzip navidad-hack-skills-<version>.zip && cd navidad-hack-skills
./install.sh --profile <profile>                      # user skills: only you
./install.sh --profile <profile> --scope workspace    # workspace skills: everyone (workspace admin)
```

Add `--clean` when upgrading, to remove files deleted since the previous version.

Manual alternative: copy both folders into `/Workspace/Users/<you>/.assistant/skills/` (or `/Workspace/.assistant/skills/`) with the workspace file browser. Keep each skill in its own folder, with `SKILL.md` at its root.

## Use

1. Open a **new** Genie Code chat (hard-refresh the browser tab if the skills don't show up).
2. Type: `Set up the Navidad hack workbench in catalog <catalog>` (or `@navidad-hack-infra`).
3. Say "yes". Genie Code runs the four steps in a serverless notebook and replies with the app link.
4. Then: `@navidad-hack-build-assistant let's build something`.

Each participant gets `navidad_<user>` (schema), `navidad-<user>` (Lakebase project) and `navidad-<user>-app` (app). Setup at full scale takes about 25 minutes, mostly writing ~4M journey rows.

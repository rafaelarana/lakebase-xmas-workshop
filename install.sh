#!/usr/bin/env bash
# Install the Navidad hack skills into a Databricks workspace for Genie Code.
#
#   ./install.sh --profile <cli-profile> [--scope user|workspace] [--clean]
#
#   --scope user       /Workspace/Users/<you>/.assistant/skills/   (default; only you)
#   --scope workspace  /Workspace/.assistant/skills/               (everyone; workspace admin)
#   --clean            delete each skill folder before uploading (removes stale files)
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="" SCOPE=user CLEAN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --clean) CLEAN=1; shift ;;
    -h|--help) sed -n 2,9p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PROFILE" ] || { echo "--profile is required (never defaulted)" >&2; exit 2; }

SKILLS=(navidad-hack-infra navidad-hack-build-assistant)
case "$SCOPE" in
  user)
    USER_NAME=$(databricks current-user me --profile "$PROFILE" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')
    ROOT="/Workspace/Users/$USER_NAME/.assistant/skills" ;;
  workspace) ROOT="/Workspace/.assistant/skills" ;;
  *) echo "--scope must be user or workspace" >&2; exit 2 ;;
esac

databricks workspace mkdirs "$ROOT" --profile "$PROFILE"
for skill in "${SKILLS[@]}"; do
  [ -f "$skill/SKILL.md" ] || { echo "missing $skill/SKILL.md" >&2; exit 1; }
  if [ "$CLEAN" = 1 ]; then
    databricks workspace delete "$ROOT/$skill" --recursive --profile "$PROFILE" 2>/dev/null || true
  fi
  databricks workspace import-dir "$skill" "$ROOT/$skill" --overwrite --profile "$PROFILE" >/dev/null
  echo "installed $skill → $ROOT/$skill"
done
echo "Start a new Genie Code chat (hard-refresh the tab if the skills look stale)."

#!/usr/bin/env bash
# Builds the shareable workshop zip for ONE language: ONLY what a partner needs to share and import.
#   build_zip.sh [es|en]   es (default) → dist/navidad-workshop.zip       (deck *-es.html, README.md)
#                          en           → dist/lakebase-xmas-workshop.zip (deck *-en.html, README.en.md → README.md)
#   workshop/deck/            the presentation (self-contained HTML, opens offline)
#   workshop/notebooks/       00–08 + 99 (Workspace → Import → File, keep the folder structure)
#   workshop/sql/             SQL for the Lakebase SQL editor
#   workshop/README.md        how to import and run
#   navidad-hack-infra/       scripts, SQL, app templates, road graphs (imported by the notebooks)
#   navidad-hack-build-assistant/  Genie Code skill (installed by 02_instalar_skills)
# Excluded: tests, tools, CLI installers/packagers, DAB config, dist, repo-level docs.
set -euo pipefail
cd "$(dirname "$0")/.."
# default edition: es in the bilingual master; in a one-language public repo, the language of its only deck
DEFAULT=es; ls workshop/deck/*-es.html >/dev/null 2>&1 || DEFAULT=en
LANG_ED="${1:-$DEFAULT}"
RENAME=0
case "$LANG_ED" in
  es) NAME=navidad-workshop;       DROP='^workshop/(deck/.*-en\.html|README\.en\.md)$' ;;
  en) NAME=lakebase-xmas-workshop
      if [ -f workshop/README.en.md ]; then DROP='^workshop/(deck/.*-es\.html|README\.md)$'; RENAME=1
      else DROP='^workshop/deck/.*-es\.html$'; fi ;;   # public EN repo: README.md is already English
  *) echo "usage: $0 [es|en]" >&2; exit 2 ;;
esac
OUT=dist/$NAME
rm -rf "$OUT" "dist/$NAME.zip" && mkdir -p "$OUT"
{ git ls-files; git ls-files --others --exclude-standard; } | sort -u \
  | grep -E '^(LICENSE$|NOTICE$|workshop/(deck|notebooks|sql)/|workshop/README(\.en)?\.md$|navidad-hack-infra/|navidad-hack-build-assistant/)' \
  | grep -v -E "$DROP" \
  | grep -v -E '/(tests|tools|__pycache__|\.pytest_cache)/' \
  | while read -r f; do [ -f "$f" ] && mkdir -p "$OUT/$(dirname "$f")" && cp "$f" "$OUT/$f"; done
[ "$RENAME" = 1 ] && mv "$OUT/workshop/README.en.md" "$OUT/workshop/README.md"
(cd dist && zip -qr "$NAME.zip" "$NAME" -x '*.DS_Store')
echo "dist/$NAME.zip ($(du -h "dist/$NAME.zip" | cut -f1), $(find "$OUT" -type f | wc -l | tr -d ' ') files)"

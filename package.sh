#!/usr/bin/env bash
# Build a deployable bundle of the Navidad hack skills for Genie Code.
# Runs the tests first, then copies runtime files only (no tests/, tools/, caches)
# into dist/navidad-hack-skills/ and zips it with install.sh and INSTALL.md.
set -euo pipefail
cd "$(dirname "$0")"

SKILLS=(navidad-hack-infra navidad-hack-build-assistant)
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
PY="${PY:-.venv/bin/python}"
OUT=dist/navidad-hack-skills

for skill in "${SKILLS[@]}"; do
  (cd "$skill" && "../$PY" -m pytest -q -p no:cacheprovider) >/dev/null \
    || { echo "tests failed in $skill" >&2; exit 1; }
done

rm -rf dist && mkdir -p "$OUT"
for skill in "${SKILLS[@]}"; do
  rsync -a --exclude tests/ --exclude tools/ --exclude __pycache__/ \
    --exclude .DS_Store --exclude .pytest_cache/ --exclude copied-apps/ "$skill/" "$OUT/$skill/"
done
cp install.sh INSTALL.md "$OUT/"
echo "$VERSION" > "$OUT/VERSION"
(cd dist && zip -qr "navidad-hack-skills-$VERSION.zip" navidad-hack-skills)
echo "dist/navidad-hack-skills-$VERSION.zip"
(cd dist && unzip -l "navidad-hack-skills-$VERSION.zip" | tail -1)

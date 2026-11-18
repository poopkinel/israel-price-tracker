#!/usr/bin/env bash
# Copy the running archive log(s) from $ARCHIVE_LOG_DIR into the repo, commit and push.
# Called every 15 minutes during the run and once at the end, so a cancelled run loses little.
set -u
[ -d "${ARCHIVE_LOG_DIR:-}" ] || exit 0
rm -f .git/index.lock
mkdir -p data/archive
cp "$ARCHIVE_LOG_DIR"/*.csv data/archive/ 2>/dev/null || exit 0
git add data/archive
git diff --cached --quiet && exit 0
git commit -q -m "data: archive log $(date -u +%F)"
for i in 1 2 3 4 5; do
  git pull -q --rebase && git push -q && exit 0
  sleep 15
done
exit 1

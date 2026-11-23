#!/usr/bin/env bash
# Merge the running archive log(s) from $ARCHIVE_LOG_DIR into the repo, commit and push.
# Called every 15 minutes during the run and once at the end, so a cancelled run loses little.
#
# The run's log is merged into the latest main rather than copied over it: the checkout can be
# older than main (a queued run starts from the commit it was scheduled on), and copying then
# rebasing ended in a merge conflict that blocked every later sync of that run.
set -u
[ -d "${ARCHIVE_LOG_DIR:-}" ] || exit 0
ls "$ARCHIVE_LOG_DIR"/*.csv >/dev/null 2>&1 || exit 0
rm -f .git/index.lock
git rebase --abort 2>/dev/null || true
for i in 1 2 3 4 5; do
  # nothing else in this checkout is edited during an archive run, so matching main is safe
  if git fetch -q origin main && git reset -q --hard FETCH_HEAD; then
    python -m tracker.archive --merge-log "$ARCHIVE_LOG_DIR" || exit 1
    git add data/archive
    git diff --cached --quiet && exit 0
    git commit -q -m "data: archive log $(date -u +%F)"
    git push -q origin HEAD:main && exit 0
  fi
  sleep 15
done
exit 1

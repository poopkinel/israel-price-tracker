# Agent log

Memory of the daily standing agent. Newest entries first inside each section.

## Current state

_Updated 2026-11-23 (run at about 04:45 UTC)._

- **Collection: healthy for 20 of 24 stores.** The 23 Nov collection ran (triggered by my push, data commit `007fe5a`): 20 stores ok, 71,612 products seen, 21,029 on sale. The same four stores have failed on every run since tracking began on 27 Oct and have never produced data: `cwc` (עולם הקולנוע והחשמל), `payngo` (מחסני חשמל) and `drugstore` (דראגסטור) return HTTP 403; `cookbake` (קוק אנד בייק) is blocked by its robots.txt. See "Waiting on you" item 3.
- **Scheduled runs start hours late.** GitHub starts the 00:15 UTC collection at about 07:00 UTC, and the archive runs (02:30, 09:00, 15:00) at about 10:00, 17:00 and 21:00 UTC. Archive runs take 5 hours and share one concurrency group, so the third run always queues behind the second.
- **Archiving: a logging bug fixed today.** The night archive run failed on 21 Nov and 22 Nov (the failure of 17 Nov looks the same, not verified). The pages were submitted to the Wayback Machine but the log rows never reached the repo: 905 submitted + 530 "recent" on 21 Nov, and 903 submitted on 22 Nov. Cause and fix are under "Decisions I made". The first runs to use the fix are those of 23 Nov; the next agent run should confirm the night run ends green and its rows are in `data/archive/2026-11-23.csv`.
- **Archive pace** is unchanged: about 180 pages an hour, 1,700 to 2,700 logged a day (plus the unlogged ones above), against about 5,400 to 6,600 due a day.
- The 16:53 UTC archive run on 22 Nov ended "cancelled" at 21:16 UTC after 4h23m. Its job log is no longer available, so the reason is unknown. Its rows up to 21:08 are in the repo.

## Queue (next work, top first)

1. Confirm the archive log fix in production (see above). If a run still fails, read its job log first.
2. Recover the unlogged submissions of 17, 21 and 22 Nov: the snapshots exist in the Wayback Machine, only our log rows are missing. Check whether they can be listed (CDX API per URL) and prepare a script on a branch. Adding rows to `data/archive/` afterwards is a change to collected data, so it needs the owner's answer before it is run.
3. Check a sample of the longest discounts (products on sale since 27 Oct) against their archived snapshots: does the page a buyer sees show the same "before" price as the data?
4. REPORT.md clarity: the 30+/60+/90+ columns are all zero until 26 Nov at the earliest; consider a note or a "since the first day" column. Wording only, no change to what is measured.
5. Document per-store caveats in the README (the four stores with no data; Fox, Laline and others restart their discounts in bulk).
6. Tests: none cover `todays_targets`, and `tracker/sync_archive_log.sh` is only tested by hand (two clones of a throwaway remote).

## Decisions I made

- **2026-11-23: the archive log sync merges instead of overwriting** (`d625126`). A queued archive run checks out the commit it was scheduled on, which is older than main by the time it starts. It seeded its log from that old copy, copied it over the repo's file and rebased, which conflicted with the rows the previous run had pushed. The checkout then stayed stuck mid-rebase and every later sync of that run failed. `tracker/sync_archive_log.sh` now resets the runner's checkout to the latest main, appends only the rows main does not have (`archive.merge_logs`, two new tests), and aborts any half-finished rebase. Existing rows are never rewritten.
- **2026-11-23: the archive workflow checks out the latest main** (`d2e6ad7`, `ref: main` on the checkout step). Without it a queued run did not know which pages the previous run had already saved that day and could submit them again. No change to schedules or to what is archived.
- 2026-11-23: created this log, seeded from the owner's notes of 22 Nov.

## Waiting on you

1. **Fourth daily archive run** (proposed 19 Nov, unanswered). Would raise the pace to about 3,300 pages a day. Options: add it, or leave three runs. Recommendation: add it. Note from 23 Nov: runs already queue behind each other because GitHub starts them late, so the gain may be smaller than estimated. Blocked: nothing; the pace stays as is.
2. **Archive priority rule** (proposed 19 Nov, unanswered). Products on discount for 14 days or more are archived every three days, the rest less often. Options: adopt it, or keep every on-sale product on the three-day cycle. Recommendation: adopt it, since the current pace cannot cover everything. Blocked: nothing; about half of the due pages are skipped each day meanwhile.
3. **Four stores that have never returned data** (raised 23 Nov, low priority). `cwc`, `payngo` and `drugstore` answer 403 to the tracker; `cookbake` disallows the endpoint in robots.txt. Options: (a) leave them listed as failing; (b) remove them from `stores.json` and the report; (c) replace them with other stores. Getting data from them as they are would mean changing how the tracker identifies itself or ignoring robots.txt, which I do not recommend. Recommendation: (b), and say "20 stores" in the README. Blocked: nothing.

## Weekly findings

### Week to 22 Nov 2026 (day 27 of tracking; first note, so nothing to compare with)

- 20,594 products are on sale across the 20 working stores, out of about 71,900 tracked (29%).
- **14,515 of them (70%) have been on sale every day since tracking began on 27 Oct.** The longest possible streak, 27 days, is therefore also the most common one. The 30-day column in REPORT.md will first fill on 26 Nov.
- Most products on sale since day one: Home Center 6,491 (of 7,082 on sale), Betili 4,577 (of 4,896), Factory 54 747 (of 761), Mega Sport 680, Ronen Chen 441, Gertrud 324, Gadgetline 278.
- Stores where nearly everything on sale has been on sale since day one: Princessa (140 of 140), Factory 54 (98%), Gadgetline (98%), Betili (93%), Home Center (92%), Gertrud (91%).
- Stores that restart discounts in bulk, so streaks are short although most of the catalogue is on sale: Fox (2,107 of 2,327 sale items restarted on 16 Nov), Laline (all 372 products on sale, 364 of them since 17 Nov), Fox Home (518 restarted on 16 Nov, 457 on 2 Nov), Neta Efrati (206 on 20 Nov), Ronen Chen (181 on 22 Nov). Whether a restart means the full price was charged for a day, or the data briefly dropped the "before" price, is not known yet (queue item 3).
- iDigital and Shilav show no discounts in the data (Shilav is a known undercount); New Pharm shows one.

## Run history

- **2026-11-23**: Health check: collection of 22 Nov ok (20 of 24 stores); night archive run failed. Fixed the archive log sync (`d625126`, `d2e6ad7`); 13 tests pass. The push also triggered the 23 Nov collection, which succeeded. Created this log and the first weekly note. No `add_repo` tool was available in this run; the repo was already cloned with push access, so I worked in that clone.

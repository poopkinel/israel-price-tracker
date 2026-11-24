# Agent log

Memory of the daily standing agent. Newest entries first inside each section.

## Current state

_Updated 2026-11-24 (run at about 04:45 UTC, before the day's collection, which GitHub starts at about 07:00 UTC)._

- **Collection: 19 of 24 stores on the last run (23 Nov, 07:46 UTC); 71,276 products seen, 20,933 on sale.** The four stores that never worked still fail (`cwc`, `payngo`, `drugstore`: HTTP 403; `cookbake`: robots.txt), see "Waiting on you" item 3.
- **New Pharm (`newpharm`) failed for the first time: HTTP 403** on the scheduled run of 23 Nov. It had answered normally three hours earlier (the extra collection my push triggered at 04:45 UTC) and on every day before. Two things to know: (1) on 22 Nov all of its 80 discounts ended at once (the "before" price became equal to the price) and its count dropped from 340 to 337, so the site changed something that weekend; (2) one failed day does not break anything, the store's data simply stays as of 23 Nov. Not diagnosable from the agent's sandbox (store sites are not reachable from it). Watch it: queue item 1.
- **Archiving: the log fix of 23 Nov works.** Both runs since then ended green (10:59 to 16:00 UTC and 20:52 to 01:49 UTC), all their rows are in `data/archive/2026-11-23.csv` with no duplicate rows, and the run that started at 01:59 UTC today is logging normally every 15 minutes.
- **Archive pace is a flat 180 submissions an hour**, in every full hour of every run (a limit on the Wayback Machine's side). Three runs of five hours give at most 2,700 a day. Logged: 1,674 on 22 Nov, 1,846 on 23 Nov (1,796 submitted, 50 errors). Only two runs fit into a log day because of the late starts; the third lands on the next day's log.
- **Pages due: about 7,000 a day** (6,851 to 7,196 by day of the cycle; 20,963 on-sale products in all), of which about 5,400 a day are products discounted for 14 days or more. See the note of 24 Nov under "Waiting on you" items 1 and 2.
- The 50 errors of 23 Nov were all "crawling this host is paused" from the Wayback Machine (Home Center 30, Gertrud 17, Neta Efrati 3). Such pages stay due and are retried by the next run of the day; 20 of them were submitted later the same day.
- Pushing a change under `tracker/`, `stores.json` or `collect.yml` to main starts an extra collection at once (the workflow's `push` trigger). It is harmless to the data (a second run on the same day only records real changes) but it is a second round of requests to every store, so keep such pushes few and bundle them. Changes to `tests/`, `agent/` and `README.md` do not trigger it.
- The agent's sandbox cannot reach web.archive.org or the store sites. Anything that needs them has to run inside GitHub Actions.

## Queue (next work, top first)

1. New Pharm: check the collections of 24 Nov onward. If it answers again, check whether its discounts are really gone (0 on sale since 22 Nov, 81 before). If it returns 403 three days in a row, add it to "Waiting on you" item 3 as a fifth blocked store.
2. Check a sample of the longest discounts (on sale since 27 Oct) against their archived snapshots: does the page a buyer sees show the same "before" price as the data? It cannot run from the agent's sandbox. Plan: a manual-only workflow (`workflow_dispatch`, no schedule) that looks up about 50 products in the Wayback Machine and prints a comparison to the job log, nothing written to `data/`.
3. REPORT.md clarity: the 30+/60+/90+ columns are all zero until 26 Nov at the earliest; add a "since the first day of tracking" column or a note. Wording only. It touches `tracker/report.py`, so the push triggers a collection: bundle it with any other `tracker/` change.
4. Document per-store caveats in the README (the four stores with no data; Fox, Laline and others restart their discounts in bulk; a store that fails for a day keeps its streaks when it returns).
5. Tests: `tracker/sync_archive_log.sh` is still only tested by hand.

## Decisions I made

- **2026-11-24: not recovering the unlogged submissions of 17, 21 and 22 Nov.** About 2,300 pages were submitted on those nights without a log row. The snapshots themselves are in the Wayback Machine and can be found by URL whenever a product is checked; only our own list of them is missing. Rebuilding it would take thousands of lookups and would then add rows to `data/archive/` after the fact. Not worth it; removed from the queue. The gap is recorded here so the low counts of those days are not misread.
- **2026-11-24: tests for which pages are due each day** (`dbe9219`). Five tests for `archive.todays_targets`: only on-sale products in today's third, every page due one day in three, products not seen for two days skipped, longest discounts first, and pages already handled today skipped while errors stay due. 18 tests pass. No change to behaviour.
- **2026-11-23: the archive log sync merges instead of overwriting** (`d625126`). A queued archive run checks out the commit it was scheduled on, which is older than main by the time it starts. It seeded its log from that old copy, copied it over the repo's file and rebased, which conflicted with the rows the previous run had pushed. The checkout then stayed stuck mid-rebase and every later sync of that run failed. `tracker/sync_archive_log.sh` now resets the runner's checkout to the latest main, appends only the rows main does not have (`archive.merge_logs`, two new tests), and aborts any half-finished rebase. Existing rows are never rewritten.
- **2026-11-23: the archive workflow checks out the latest main** (`d2e6ad7`, `ref: main` on the checkout step). Without it a queued run did not know which pages the previous run had already saved that day and could submit them again. No change to schedules or to what is archived.
- 2026-11-23: created this log, seeded from the owner's notes of 22 Nov.

## Waiting on you

1. **Fourth daily archive run** (proposed 19 Nov, unanswered). Would raise the pace to about 3,300 pages a day. Options: add it, or leave three runs. Recommendation: add it. Note from 23 Nov: runs already queue behind each other because GitHub starts them late, so the gain may be smaller than estimated. Note from 24 Nov: the limit is a flat 180 an hour, so a fourth five-hour run adds at most 900 a day (3,600 in all) and only if the four runs do not queue behind each other; about 7,000 are due. Blocked: nothing; the pace stays as is.
2. **Archive priority rule** (proposed 19 Nov, unanswered). Products on discount for 14 days or more are archived every three days, the rest less often. Options: adopt it, or keep every on-sale product on the three-day cycle. Recommendation: adopt it, since the current pace cannot cover everything. Note from 24 Nov: counted today, products discounted for 14 days or more are about 5,400 of the 7,000 pages due each day, so this rule alone does not fit the pace either (2,700 a day now, 3,600 with a fourth run). At the present pace the realistic cycles are: every on-sale product about every 8 days, or with a fourth run about every 6 days. A version that fits: long discounts (14 days or more) every 6 days, the rest every 12, with four runs. The cycle length is your call. Blocked: nothing; about 60% of the due pages are skipped each day meanwhile, and which ones are saved is decided by discount age (oldest first).
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
- **2026-11-24**: Health check: collection of 23 Nov 19 of 24 (New Pharm 403 for the first time); both archive runs since the fix green, logs complete. Added tests for the daily archive targets (`dbe9219`); 18 tests pass. Dropped the recovery of unlogged rows from the queue. Measured the archive limit (180 an hour) and the pages due (about 7,000 a day, 5,400 of them long discounts) and added the numbers to the two open archive decisions. No `add_repo` tool again; worked in the existing clone.

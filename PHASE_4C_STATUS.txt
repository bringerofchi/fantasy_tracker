# Phase 4C — ESPN Source: Status

**Phase 4C Research → CLOSED** (2026-09-02)
**Phase 4C Adapter Implementation (standalone) → COMPLETE** (2026-09-02)
**Phase 4C Integration/QC (against the standalone fixture + live ESPN only) → RUN, PASSED** (2026-09-02)
**Phase 4C Tracker Integration → BLOCKED (2026-09-03): underlying tracker codebase not located, then superseded by a decision to build it — see below**
**Tracker Backend v1 (data model, SQLite storage, ingestion pipeline) → BUILT (2026-09-03)**
**Tracker Backend v1 → REVIEWED, CORRECTED (A/B/D fixes below), LIVE-VALIDATED AT FULL POPULATION → FROZEN (2026-09-03)**

Conclusion: **the ESPN adapter itself is READY FOR INTEGRATION** — it is not waiting on further ESPN ranking research; the ranking gaps documented below are established source limitations, not open questions this adapter is blocked on. The `NormalizedObservation`/`SourceAdapter`/`LocalFileSourceAdapter` tracker architecture the original brief described as already existing was never found (see "Where is the tracker codebase?" below) — rather than continue searching, the project owner made the call to build it, using the proven ESPN adapter as the first real source. That backend now exists: see "Tracker Backend v1" below.

## Backend v1 review round — findings and fixes (2026-09-03)

An independent review of the committed backend (against the three contracts: normalized data model; adapter → ingestion → SQLite; source attribution/idempotency) surfaced three real gaps, all now fixed and tested. Sequence followed: fix scoring_format uniqueness → fix the ACTUAL anomaly-vs-absence distinction → run a full-population live test → the full-population test surfaced a real, previously-untested case → survey the full population to characterize its true scope → fix the PROJECTION contract accordingly → re-run the complete suite. All 45 tests pass (`python -m pytest -v`).

1. **`scoring_format` added to the storage uniqueness key.** Every observation currently hardcodes `"PPR"`, so this was a no-op in practice — but the field existed without being protected by the UNIQUE constraint, meaning a future Standard/Half-PPR import for the same player/week would have silently overwritten a PPR row instead of coexisting with it. Fixed in `storage.py` (schema, upsert SQL, row-key logic all now include it, with the same `''`-instead-of-`NULL` handling already used for `ranking_type`). Tests: `test_storage.py::test_same_player_week_different_scoring_formats_coexist` and `test_reimporting_same_scoring_format_still_upserts`.

2. **ACTUAL "no entry" vs. "multiple entries" were indistinguishable.** Both raised `ESPNSchemaError` and were silently swallowed identically by `fetch()` — meaning a genuine duplicate/conflicting ACTUAL entry (a real integrity anomaly) would have looked exactly like an ordinary unplayed week. Fixed by adding `ESPNDataAnomalyError` in `espn_core.py`, deliberately NOT a subclass of `ESPNSchemaError`, raised specifically for the >1-match case. `find_weekly_stat_entry()` now raises `ESPNSchemaError` (0 matches, expected absence — still swallowable) or `ESPNDataAnomalyError` (>1 matches, genuine anomaly — propagates and fails the whole fetch loudly). Because the two exception types are now distinct, zero changes were needed to the existing `except ESPNSchemaError: pass` blocks — the anomaly case simply stopped being caught by them. Tests: `test_espn_core.py::TestFindWeeklyStatEntry` and `TestFetchPropagatesActualAnomaly`.

3. **PROJECTION's "fail the whole batch on one missing player" behavior was never actually tested at scale, and turned out to be wrong.** A live full-population fetch (`run_full_population_check.py`, no `player_ids` filter — every prior live test in this project scoped to 8 known-active players) failed immediately on `'Steelers D/ST'`. A follow-up survey (`survey_full_population_projection_coverage.py`, tallies success/failure per player instead of stopping at the first one) found **433 of 1090 players (~40%) lack a weekly projection for a single real week, across every position, not just D/ST.** Root causes, confirmed against raw data: the Steelers' full `stats[]` array has zero entries of any kind for that one scoring period (a bye week — not D/ST-specific), and most of the remaining failures are recognizable players off an active/relevant roster that week (ESPN's unscoped population query surfaces its whole draftable/historical player-id space, not just that week's rostered players). This is the common case at full-population scale, not a corner case, and is not data corruption.
   Fix: `parse_weekly_projection()` is now wrapped in the same per-player try/except pattern already used for ACTUAL — `ESPNSchemaError` (expected absence) is caught and that one player is skipped, `ESPNDataAnomalyError` (genuine anomaly) still propagates and aborts. This does not weaken "never fabricate a value" — that principle was always about substitution, not about how many players one absence is allowed to affect. Tests: `test_espn_core.py::TestFetchProjectionAbsenceVsAnomaly` (all four cases: missing → skipped, missing-for-everyone → empty list not exception, duplicate → anomaly propagates, valid → parsed normally), plus `test_espn_adapter.py::TestMissingDataFailsLoudly` updated to assert the new contract instead of an exception.
   Still open, not part of this fix: `defaultPositionId=9` (seen on a real player, "Scott Matlock") is unrecognized by `DEFAULT_POSITION_ID_TO_POSITION` and correctly still raises — this is a genuine gap in the position map, not an absence-vs-anomaly question, and needs its own follow-up once its meaning is confirmed.

Also caught and fixed during this round, unrelated to the contract questions above: `run_full_population_check.py` (originally named `run_full_population_test.py`) accidentally matched pytest's default `*_test.py` collection glob, so `python -m pytest -v` would have imported and executed it — live network call and all — during what's supposed to be the offline unit suite. Renamed, and `pytest.ini` now restricts collection to `test_*.py` so this class of bug can't recur regardless of future script names.

**Live confirmation, post-fix (2026-09-03):** `run_full_population_check.py` was re-run against real ESPN data after the fix above. Result: `fetch()` completed with zero exceptions across the full 1090-player population (previously: aborted on the first missing player). 657/1090 players (60.3%) produced a projection — matching the survey's prediction exactly (1090 − 433 = 657) — with the remainder correctly skipped rather than fabricated or fatal. One more small bug caught in the process: the script's own success-check still asserted the *pre-fix* rule ("success implies 100% coverage") and flagged this correct, expected outcome as `[UNEXPECTED]`. Not an adapter defect — a stale assertion in the diagnostic script itself, now fixed to match the new contract (partial coverage is expected; only zero coverage or an actual exception is flagged as suspicious).

## Backend v1: FROZEN (2026-09-03)

Frozen on the strength of more than a green test suite: a defensible, evidence-backed contract (missing source data is absence and is skipped; contradictory/duplicated source data is an anomaly and fails loudly), verified against real full-population ESPN data (1090 players, 0 exceptions, coverage matching the independently-derived prediction exactly), with two incidental defects (the scoring_format uniqueness gap and the pytest collection glob collision) caught and corrected rather than left latent.

**Going forward, this is versioned, not incrementally patched.** Any further change to the normalized data model, the ESPN adapter's parsing contract, or the storage schema is v2 work — a new review-and-decide cycle, not a cleanup pass on v1 — specifically so the evidence and contract established in this round don't silently drift. The next work is downstream: a second real source adapter (Yahoo) and cross-source integration against real data, not further speculative changes to the ESPN adapter itself.

## Tracker Backend v1 — what was built (2026-09-03)

Scope, per the project owner's explicit choices: **backend only** (no UI yet), **SQLite** for storage, built **inside the `fantasy_tracker` repo** (delivered here for you to add to it — this session has no direct GitHub write access; see delivery notes).

New files, on top of the existing ESPN adapter:

| File | Purpose |
|---|---|
| `storage.py` | SQLite schema (one `observations` table) + `save_observations()` (upsert, keyed on `source, season_id, week_number, data_type, ranking_type, source_player_id`) + `query_observations()`. |
| `ingestion.py` | `ingest(adapter, conn, season_id, week_number)` — the glue between any `SourceAdapter` and storage. Deliberately thin; all real logic lives in `espn_core.py` and `storage.py`. |
| `test_storage.py` | 9 unit tests against the storage layer directly (upsert behavior, the NULL-vs-empty-string ranking_type trap, cross-source coexistence, batch-duplicate detection). |
| `test_ingestion.py` | 5 integration tests, fixture-backed (no network) — **this is what closes out QC5b and QC6b**, see below. |
| `run_espn_ingest.py` | End-to-end script: live ESPN data → `ESPNSourceAdapter` → `ingest()` → a real `tracker.db` SQLite file you can open and inspect. Same pattern as `qc_phase4c.py` (needs your machine's internet access; can't run in the research sandbox). |

**QC5b and QC6b are no longer NOT TESTED.** They now have real, passing tests:

- **QC5b (duplicate-on-insert) → PASS.** `test_ingestion.py::TestDuplicateImportHandling` ingests the same source/season/week twice (and three times) and asserts the row count doesn't grow and every row gets upserted, not duplicated. This is a genuine DB-level guarantee now, not a documented gap.
- **QC6b (cross-source attribution) → PASS, with a disclosed caveat.** `test_ingestion.py::TestCrossSourceAttribution` proves the storage layer keeps two sources' data on the same player/week fully separate (neither overwrites the other, row counts stay independent). The "other source" here is `_FakeOtherSourceAdapter`, a ~15-line test double defined in `test_ingestion.py` itself — **not** a real Yahoo or Athletic adapter, which don't exist yet. This proves the storage layer's isolation logic is sound; it does not prove a real second data source integrates cleanly, since there isn't one yet to test against.

All 33 tests pass (19 from the original ESPN adapter suite + 9 storage + 5 ingestion): `python -m pytest -v` from this folder.

## Where is the tracker codebase?

Checked, with no result, across this work:
1. **This session's own sandbox filesystem** — searched at the start of this work (`find`/`grep` for `NormalizedObservation`, `SourceAdapter`, `LocalFileSourceAdapter` across the whole environment). Nothing.
2. **The "fantasy football" Claude project** (docs + knowledge base) — checked repeatedly across this work. Contains only the docs this session itself wrote (`espn-adapter-findings.md`, `phase-4c-status.md`, `espn-adapter-readme.md`).
3. **The `bringerofchi/fantasy_tracker` GitHub repository** — 2 commits, containing only `README.md` (title + one-line description) and `espn_adapter.zip`. Code search for all three class/type names returned nothing. Important caveat: this session never created or pushed to this repository — it appears to be a landing spot for the files delivered via download in this conversation, not a pre-existing tracker project. Its emptiness confirms the tracker isn't *there*, but says nothing about where it might actually be, since it was never a real candidate location.

**Open question for the project owner — RESOLVED (2026-09-03):** the second public repo on the account (`MLB-EDGE-LAB`) was also checked (root listing + README) and shows no trace of the architecture either, and its `mlb_edge_lab/` subfolder could not be searched further (GitHub blocks `/tree/` and `/search` paths via robots.txt for unauthenticated fetches, and a direct API request was declined). Given everything actually inspected — this repo, the Claude project, the sandbox filesystem, and MLB-EDGE-LAB's root — the project owner has confirmed the working assumption going forward: **the tracker codebase does not exist yet.** It is not a location problem to keep investigating; it's a build decision.

**Note for future sessions — README.md on GitHub was hand-edited after delivery (2026-09-03):** the version now in `bringerofchi/fantasy_tracker` is NOT byte-identical to what this session generated. It was tightened in one consistent direction — every capability claim now explicitly caveats "not yet validated against the tracker" — and gained: an explicit note that the repo's files live inside `espn_adapter.zip` rather than at the repo root, a fuller "Integration status" section enumerating the two NOT TESTED checks by name, and a "Next step" pointer. Treat the GitHub copy as canonical over any local `README.md` a future session finds bundled in an older copy of the zip.

## QC run result (2026-09-02, `qc_phase4c.py`, live production adapter, real 2026 players)

```
8 PASS, 0 FAIL, 2 NOT TESTABLE
RESULT: QC PASSED
```

All 8 testable checks passed on the first run against live ESPN data (8 real players, all 4 positions, both the 2026 preseason context and 2025 historical context). The 2 NOT TESTABLE items — duplicate-on-*insert* handling and cross-source contamination — require the tracker's DB/ingestion layer and other source adapters. As of 2026-09-03 this is no longer just "code this session wasn't given": a direct search (see "Where is the tracker codebase?" above) turned up no evidence that code exists anywhere yet. They remain open items for whoever builds or locates that code, not unresolved risk in the ESPN adapter itself — but they can't be scheduled as near-term follow-ups until that target exists.

**Reclassification note:** the run above used a three-category scheme (PASS / FAIL / NOT TESTABLE). That scheme incorrectly bucketed "ESPN doesn't expose this value for a completed season" (QC4b) as a plain PASS, which reads as "capability works" rather than "confirmed source limitation." `qc_phase4c.py` has since been updated to a four-category scheme — PASS / EXPECTED ABSENCE / FAIL / NOT TESTED — and QC4b now reports as **EXPECTED ABSENCE**, not PASS. No underlying test outcome changed (still 0 FAIL); only the reporting taxonomy did. Re-running the updated script will show **7 PASS, 1 EXPECTED ABSENCE, 0 FAIL, 2 NOT TESTED**.

## Why research is closed

The ESPN public `kona_player_info` endpoint has gone from "we think this endpoint might work" to empirically characterized and live-tested production code. Specifically, as of this date:

| Item | Status |
|---|---|
| Weekly projection ingestion | Proven |
| Weekly actual-stat ingestion | Proven |
| Overall PPR ranking | Proven |
| Current/preseason positional PPR ranking | Proven, where ESPN exposes it |
| Production HTTP path (`ESPNSourceAdapter`, real `requests` calls) | Proven live, on a normal internet-connected machine, independent of this research sandbox |
| Authentication requirement | None — confirmed with `credentials:'omit'` equivalent (a plain cookie-less `requests.Session`); no `espn_s2`/`SWID`/`Authorization` used anywhere |
| Player population retrieval | Adequately validated (1036 players via `sortDraftRanks`+`limit`, vs. a silently-truncated 50-player unfiltered default) |
| Historical weekly positional ranking | Confirmed **unavailable** from this endpoint once a season ends — a source limitation, not an implementation gap |
| In-season weekly ranking | Still an open empirical item — observed sparse/unpopulated pre-season; not implemented as anything more than "try `rankings[week]`, fail loudly if absent" |
| FLEX ranking | No native ESPN field. Explicitly out of scope for this adapter — see policy note below |

The remaining ranking limitations (historical weekly rank, in-season weekly rank, FLEX) are **source limitations**, not defects in `espn_core.py`/`espn_adapter.py`. Full detail, evidence, and the exact requests that established each row above: `FINDINGS.md` in this same delivery.

Further ESPN endpoint archaeology (hunting for an undocumented historical-ranking filter, etc.) is deprioritized: the research already showed several plausible, commonly-cited filter keys (`filterStatsForSourceIds`, `filterRanksForScoringPeriodIds`, `filterRanksForRankTypes`) do nothing at all against the live endpoint, and one (`filterStatsForTopScoringPeriodIds`) actively misleads if used as its name suggests. Diminishing returns. Revisit only if the project later establishes that historical weekly rankings are a hard requirement.

## FLEX — explicit policy, not a QC item

FLEX is **not** part of the Phase 4C Integration/QC pass. ESPN does not expose a FLEX-scoped rank anywhere in this response; `RankingType.FLEX` exists in `normalized.py`'s enum for schema completeness but `espn_core.py` never produces it — confirmed by static scan (see QC script, "FLEX never fabricated" check). Any FLEX value the tracker ever shows a user must be computed downstream (pooling RB/WR/TE by projection or overall rank) and **labeled as tracker-derived**, never presented as an ESPN-sourced ranking. This is a source/modeling policy decision for wherever that pooling logic eventually lives, not something this adapter should decide or silently do on its own.

## What Integration/QC covers (this phase)

1. Run the production `ESPNSourceAdapter` (live HTTP, not the fixture-based `LocalFileSourceAdapter`) against a small set of real 2026 players across QB/RB/WR/TE.
2. Confirm projections match the `NormalizedObservation` contract.
3. Confirm overall and available positional rankings land with the correct `ranking_type`.
4. Confirm missing ESPN data (completed-season positional rank, not-yet-played actuals) stays absent rather than getting synthesized as zero or a fabricated value.
5. Confirm the adapter's own output is idempotent (a precondition for safe re-import) — full duplicate-on-insert handling is a database/ingestion-layer concern outside this adapter's code and is marked NOT TESTED until that layer is available for review.
6. Confirm every observation this adapter produces is consistently attributed (`source="espn"`) and structurally isolated from other sources — full "alongside other sources" testing is marked NOT TESTED until the other source adapters and the shared ingestion pipeline are available.

`qc_phase4c.py` reports each check as one of four outcomes, deliberately kept distinct so a source limitation can never masquerade as either a capability proof or a real defect:

- **PASS** — the capability works, verified against live data.
- **EXPECTED ABSENCE** — ESPN doesn't expose the requested value (e.g. positional ranking for a completed season); the adapter correctly returns nothing rather than fabricating a value.
- **FAIL** — our adapter/database behavior is wrong.
- **NOT TESTED** — the capability hasn't been empirically established yet, either because the code needed to test it (DB/ingestion layer, other adapters) isn't available to this session, or because the real-world condition needed to observe it (e.g. an in-season week) hasn't occurred yet.

This distinction matters most for positional ranking and FLEX specifically: neither should ever show up as a plain PASS, since both are, at best, "works where ESPN provides it" rather than fully general capabilities.

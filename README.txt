# NFL Fantasy Data Tracker — Backend v1

A normalized, source-independent data model for NFL fantasy football data (projections, actual results, PPR rankings), backed by SQLite, with ESPN as the first working source adapter.

**Status:** backend (data model + storage + ingestion + one real source adapter) is built and tested, and has been through one full review-and-correction round against real full-population data (2026-09) — see `PHASE_4C_STATUS.md` for what that round found and fixed. No UI yet — that's an explicit, deliberate scope decision, not an oversight.

## Architecture

```
SourceAdapter.fetch(season_id, week_number)  →  list[NormalizedObservation]
                                                        │
                                                        ▼
                                         ingestion.ingest(adapter, conn, ...)
                                                        │
                                                        ▼
                                    storage.py  →  SQLite (observations table)
```

- **`normalized.py`** — the source-independent contract. `NormalizedObservation`, `PlayerIdentity`, `Position`, `DataType`, `RankingType`, and the `SourceAdapter` abstract base every source implements. Nothing source-specific belongs here, ever.
- **`espn_core.py` / `espn_teams.py` / `espn_adapter.py` / `local_file_adapter.py`** — the ESPN source adapter. All ESPN-specific quirks (field names, the `statSourceId`/`scoringPeriodId` scheme, which filters are real vs. no-ops) are isolated here. See `FINDINGS.md` for the research that established every one of these facts against live data, not assumption.
- **`storage.py`** — SQLite persistence. One `observations` table; a fact is uniquely identified by `(source, season_id, week_number, data_type, ranking_type, source_player_id, scoring_format)`, and re-ingesting the same fact upserts instead of duplicating.
- **`ingestion.py`** — the thin glue that calls an adapter and hands its output to storage.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -v                       # 45 tests, no network needed (all fixture/in-memory)
python run_espn_ingest.py                 # live ESPN data (8 known players) → a real tracker.db, needs internet
python run_full_population_check.py       # live ESPN data, FULL population (~1000 players), needs internet
python survey_full_population_projection_coverage.py   # live diagnostic: coverage by position, needs internet
```

`run_espn_ingest.py` ingests the same week twice on purpose and reports the counts, so you can watch the duplicate-handling guarantee hold on real data, not just in tests. `run_full_population_check.py` and the survey script exist because "8 known active players" and "ESPN's full ~1000-player population" turned out to behave differently — see "What's proven vs. not" below.

Note: none of these live scripts are named `test_*.py` or `*_test.py` — pytest's default collection glob matches both patterns, and a script with top-level network calls and an interactive prompt getting silently imported and executed during `python -m pytest` is a real bug that happened once already during this project (see `PHASE_4C_STATUS.md`). `pytest.ini` now restricts collection to `test_*.py` to close that off for good; keep future live/manual scripts off that naming pattern regardless.

## Adding a second source

1. Implement `SourceAdapter` (see `espn_adapter.py` for the reference — the only hard requirement is `fetch(season_id, week_number) -> list[NormalizedObservation]`, with a unique `source_name`).
2. That's it for storage/ingestion — `ingest()` and `storage.py` are already source-agnostic; nothing about them needs to change.
3. Cross-source coexistence is exercised by `test_ingestion.py::TestCrossSourceAttribution`, currently against a fake test-double source. Once a real second adapter exists, add it there (or a parallel test) to validate against real data instead of the test double — see `PHASE_4C_STATUS.md` for exactly what's proven vs. not yet.

## What's proven vs. not

**Proven, against live data:**
- ESPN weekly projections, weekly actuals, overall PPR rank, and current/preseason positional PPR rank (see `FINDINGS.md`)
- SQLite upsert semantics: re-ingesting the same fact never duplicates a row (`test_ingestion.py::TestDuplicateImportHandling`)
- Two sources' data for the same player/week coexist without contaminating each other (`test_ingestion.py::TestCrossSourceAttribution` — against a test double; see caveat below)
- Two different scoring formats for the same player/week coexist without one overwriting the other (`test_storage.py::test_same_player_week_different_scoring_formats_coexist`)
- At full-population scale (~1000 players), ~40% legitimately lack a weekly projection for a given week — confirmed root causes include a team's bye week and players off an active/relevant roster that week, not data corruption. The adapter's contract (2026-09 review) is that this absence is expected: that player is skipped, the rest of the batch is unaffected, and nothing is fabricated. A genuine anomaly (e.g. duplicate/conflicting stat entries for what should be one fact) is a different, still-fatal case — see `ESPNDataAnomalyError` in `espn_core.py` and `test_espn_core.py`.

**Documented source limitations (not adapter/backend defects):**
- ESPN doesn't retain historical weekly positional rankings once a season ends
- In-season weekly consensus ranking is unverified — pre-season data showed it sparse/unpopulated; needs re-checking once a season is actually underway
- FLEX has no ESPN-provided field at all; any FLEX ranking has to be computed downstream from RB/WR/TE and explicitly labeled as tracker-derived, not ESPN-sourced
- `defaultPositionId=9` (seen live on the full-population survey, e.g. "Scott Matlock") is not in the adapter's position map and correctly raises rather than guessing — unidentified as of this review; anyone parsing the true full, unscoped population should expect to see it and may need to extend `DEFAULT_POSITION_ID_TO_POSITION` once its meaning is confirmed

**Not yet built:**
- A UI (out of scope for this v1, by design)
- A real second source adapter (Yahoo, The Athletic, or otherwise) — `TestCrossSourceAttribution` currently proves the storage layer's isolation logic works, using a small fake adapter defined only for that test; it does not prove a real second integration
- In-season live validation of ESPN's weekly ranking behavior (can't be tested until a season is actually in progress)

## Repository history

This repo previously held only the standalone ESPN adapter (as `espn_adapter.zip`) while the question of whether a pre-existing tracker codebase existed was investigated. It was checked against this repo, the `MLB-EDGE-LAB` repo, a Claude project's docs, and a research sandbox — no trace of the described architecture was found anywhere. Rather than continue searching, the decision was made to build the backend fresh, using the already-proven ESPN adapter as its first real component. Full history in `PHASE_4C_STATUS.md`.

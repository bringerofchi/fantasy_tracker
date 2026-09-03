# NFL Fantasy Data Tracker — Backend v1

A normalized, source-independent data model for NFL fantasy football data (projections, actual results, PPR rankings), backed by SQLite, with ESPN as the first working source adapter.

**Status:** backend (data model + storage + ingestion + one real source adapter) is built and tested. No UI yet — that's an explicit, deliberate scope decision, not an oversight. See `PHASE_4C_STATUS.md` for the full history, including the research phase that proved the ESPN adapter works before this backend was built around it.

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
- **`storage.py`** — SQLite persistence. One `observations` table; a fact is uniquely identified by `(source, season_id, week_number, data_type, ranking_type, source_player_id)`, and re-ingesting the same fact upserts instead of duplicating.
- **`ingestion.py`** — the thin glue that calls an adapter and hands its output to storage.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -v              # 33 tests, no network needed (all fixture/in-memory)
python run_espn_ingest.py        # live ESPN data → a real tracker.db file, needs internet
```

`run_espn_ingest.py` ingests the same week twice on purpose and reports the counts, so you can watch the duplicate-handling guarantee hold on real data, not just in tests.

## Adding a second source

1. Implement `SourceAdapter` (see `espn_adapter.py` for the reference — the only hard requirement is `fetch(season_id, week_number) -> list[NormalizedObservation]`, with a unique `source_name`).
2. That's it for storage/ingestion — `ingest()` and `storage.py` are already source-agnostic; nothing about them needs to change.
3. Cross-source coexistence is exercised by `test_ingestion.py::TestCrossSourceAttribution`, currently against a fake test-double source. Once a real second adapter exists, add it there (or a parallel test) to validate against real data instead of the test double — see `PHASE_4C_STATUS.md` for exactly what's proven vs. not yet.

## What's proven vs. not

**Proven, against live data:**
- ESPN weekly projections, weekly actuals, overall PPR rank, and current/preseason positional PPR rank (see `FINDINGS.md`)
- SQLite upsert semantics: re-ingesting the same fact never duplicates a row (`test_ingestion.py::TestDuplicateImportHandling`)
- Two sources' data for the same player/week coexist without contaminating each other (`test_ingestion.py::TestCrossSourceAttribution` — against a test double; see caveat below)

**Documented source limitations (not adapter/backend defects):**
- ESPN doesn't retain historical weekly positional rankings once a season ends
- In-season weekly consensus ranking is unverified — pre-season data showed it sparse/unpopulated; needs re-checking once a season is actually underway
- FLEX has no ESPN-provided field at all; any FLEX ranking has to be computed downstream from RB/WR/TE and explicitly labeled as tracker-derived, not ESPN-sourced

**Not yet built:**
- A UI (out of scope for this v1, by design)
- A real second source adapter (Yahoo, The Athletic, or otherwise) — `TestCrossSourceAttribution` currently proves the storage layer's isolation logic works, using a small fake adapter defined only for that test; it does not prove a real second integration
- In-season live validation of ESPN's weekly ranking behavior (can't be tested until a season is actually in progress)

## Repository history

This repo previously held only the standalone ESPN adapter (as `espn_adapter.zip`) while the question of whether a pre-existing tracker codebase existed was investigated. It was checked against this repo, the `MLB-EDGE-LAB` repo, a Claude project's docs, and a research sandbox — no trace of the described architecture was found anywhere. Rather than continue searching, the decision was made to build the backend fresh, using the already-proven ESPN adapter as its first real component. Full history in `PHASE_4C_STATUS.md`.

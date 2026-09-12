# Phase 4C — ESPN Source: Status

**Phase 4C Source Research → CLOSED** (2026-09-03)
**Phase 4C Integration/QC → ACTIVE** (2026-09-03)

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
5. Confirm the adapter's own output is idempotent (a precondition for safe re-import) — full duplicate-on-insert handling is a database/ingestion-layer concern outside this adapter's code and is marked NOT TESTABLE until that layer is available for review.
6. Confirm every observation this adapter produces is consistently attributed (`source="espn"`) and structurally isolated from other sources — full "alongside other sources" testing is marked NOT TESTABLE until the other source adapters and the shared ingestion pipeline are available.

See `qc_phase4c.py` for the runnable checks and their PASS/FAIL/NOT TESTABLE output. A missing ESPN positional ranking on a completed season is an **expected source limitation** and is asserted as such, not reported as a failure.

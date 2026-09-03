# ESPN Fantasy Endpoint — Research Findings

**Status: SOURCE RESEARCH CLOSED / ADAPTER PRODUCTION PATH VALIDATED.** Weekly projection/actual ingestion and overall PPR ranking are proven end-to-end. Current/preseason positional PPR ranking is proven where ESPN exposes it. ESPN does not expose historical weekly rankings through this endpoint, FLEX has no native field, and in-season weekly consensus ranking remains an explicitly unverified source capability. These are documented source limitations, not adapter defects — the adapter fails loudly rather than fabricating unsupported rankings. **Conclusion: ESPN adapter is READY FOR INTEGRATION/QC**, not waiting on further ranking research.

**2026-09-02 update — `ESPNSourceAdapter` (the actual `requests`-based production code, not just browser `fetch()`) was executed live** from a normal internet-connected machine: `adapter.fetch(season_id=2025, week_number=5, player_ids=[4429795])` (Jahmyr Gibbs) returned `PROJECTION=22.06789034`, `ACTUAL=16.7`, `RANKING(OVERALL)=5.0`, and correctly produced **no** position-ranking observation — all exactly matching this document's predictions, including the documented absence of `rankings` for a completed season. This closes the one gap noted below ("could not be executed live from this sandbox"): the live HTTP path is now confirmed working end-to-end, with zero auth, outside the research sandbox. A subsequent Integration/QC pass (`qc_phase4c.py`, same day) ran 8 checks against live 2026 data across QB/RB/WR/TE and passed all of them — see `PHASE_4C_STATUS.md`.

All requests in this document were made from a real browser (`fetch()`, no page reload between most of them) directly against ESPN's live endpoint, with **zero cookies and zero auth headers** — `credentials: 'omit'` was used explicitly for the final confirmation request. No `espn_s2`, `SWID`, or `Authorization` header was ever sent by any request in this investigation, and none appear in the adapter code.

Endpoint used throughout:
```
GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/3?view=kona_player_info
Header: x-fantasy-filter: <JSON>
```

## A. Weekly projection filter

**Proven, and the brief's premise turned out to be slightly wrong.** Neither `filterStatsForExternalIds` nor `filterStatsForTopScoringPeriodIds` is required (or even correct) for selecting a specific week's projection:

- With **no stats filter at all** (just `players.filterIds` to scope to one player), a single request already returns that player's *entire* `stats[]` history for the requested season — 38 entries for a full 2025 season: one **projected** (`statSourceId=1`) and one **actual** (`statSourceId=0`) entry for every week 1–18, plus 3 season-aggregate entries. This was confirmed identically for a QB (Josh Allen), RB (Jahmyr Gibbs), WR (Ja'Marr Chase), and TE (Trey McBride).
- `filterStatsForTopScoringPeriodIds: {value: N}` **was tested and does something, but not what its name implies**: it returned only the N *most recent* `statSourceId=0` (actual) entries and silently dropped every projected entry. Using it would have caused exactly the bug the brief warns against (silently losing projections). **This adapter never sends it.**
- `filterStatsForSourceIds: {value:[1]}` was tested and had **zero observable effect** — byte-identical response to omitting it entirely. Not a working filter at this endpoint/version; not used.

**Adapter design decision:** fetch the full unfiltered `stats[]` array per player and select the exact `(seasonId, scoringPeriodId, statSourceId, statSplitTypeId)` tuple client-side (see `espn_core.find_weekly_stat_entry`). This trades a larger per-request payload for not depending on an ESPN filter we could not fully characterize.

## B. scoringPeriodId ↔ NFL week

**Proven for the weekly (`statSplitTypeId=1`) entries.** `scoringPeriodId` runs 1–18 for a season, one entry per week, matching the NFL's 18-week regular season exactly, with `scoringPeriodId=0` reserved for season-aggregate entries (`statSplitTypeId=0`). Cross-checked across 4 different positions/players — the set of populated `scoringPeriodId`s (1–18) was identical for all four. Soft corroboration: Jahmyr Gibbs' week-8 2025 *projected* total was `0`, consistent with a bye week.

Not independently verified: the literal calendar mapping (e.g. "week 5" = a specific date range) — only that `scoringPeriodId` N behaves as a stable, sequential per-week bucket consistent across players.

## C. Concrete proven example (2025, week 5 — one per required position)

Directly from a live response, `players[].player.stats[]`, filtered to `seasonId=2025, scoringPeriodId=5, statSplitTypeId=1`:

| Player | Pos | statSourceId=1 (projected) `appliedTotal` | statSourceId=0 (actual) `appliedTotal` | projected `id` | actual `id` |
|---|---|---|---|---|---|
| Josh Allen | QB | 22.29365337 | 19.42 | `1120255` | `01401772922` |
| Jahmyr Gibbs | RB | 22.06789034 | 16.7 | `1120255` | `01401772854` |
| Ja'Marr Chase | WR | 16.05840835 | 29.0 | `1120255` | `01401772854` |
| Trey McBride | TE | 15.46032864 | 9.1 | `1120255` | `01401772747` |

`id` pattern: projected entries are `"1" + seasonId + scoringPeriodId` (`1`+`2025`+`5` = `1120255`, shared across players for the same week since it's not game-specific). Actual entries are `"0" + externalId`, where `externalId` is a real NFL game id (shared by players who played in the same game — Allen and... no, shared by players in the *same* game, e.g. Gibbs and Chase happened to both show `401772854` here only if they played each other that week; not asserted further).

This exact table is encoded as regression assertions in `test_espn_adapter.py::TestWeeklyProjectionKnownPlayers`.

## D. PPR ranking — partially proven, real gaps remain

Two *different* fields both carry "PPR rank," and they answer different questions:

1. **`player.draftRanksByRankType.PPR.rank`** — a single number, not scoped by week or position. Proven to be the **cross-position overall consensus rank** (Jahmyr Gibbs = 1, Ja'Marr Chase = 3, Trey McBride = 21, Josh Allen = 26, for 2026 preseason — matches real-world 2026 preseason consensus ordering). Maps to `ranking_type="overall"`.
2. **`player.rankings[periodKey]`**, an array of per-source rank entries. The entry with `rankSourceId=0` is ESPN's own consensus (the others, `rankSourceId` 3/5/6/7/9/10/11/12, are individual outside experts ESPN blends in). Proven: the consensus entry's `slotId` matches the *player's own position slot*, i.e. this is a **position-scoped rank** (this player's rank among others at their own position), not overall. Maps to `ranking_type="QB"/"RB"/"WR"/"TE"`. Its `rank` field is frequently `0` (a sentinel) — the real fractional value is in `averageRank` (e.g. Gibbs: `rank=0, averageRank=1.25`).

Filters `filterRanksForScoringPeriodIds` and `filterRanksForRankTypes` were tested and had **zero observable effect** on the `rankings` object — not used.

**What is NOT proven / open gaps:**
- `rankings["1"]` (week 1, 2026 preseason) was populated but **had no `rankSourceId=0` consensus entry at all** — only individual-expert entries, all `published:false`. Whether a per-week consensus rank ever populates once the season is live is **unconfirmed**. This must be re-checked against a live in-season week before the per-week ranking path is trusted.
- For a **completed** season (2025, queried after the season ended), `rankings` came back **null/absent for every player**, regardless of filters — ESPN does not appear to retain historical weekly ranking snapshots via this endpoint. A query like "what was Ja'Marr Chase's PPR rank in week 5 2025" **cannot be answered from this field**, full stop. `draftRanksByRankType` does persist for a completed season, but it reflects that season's *preseason* consensus, not a retrospective performance-based rank.
- **`ranking_type="FLEX"` has no corresponding ESPN field anywhere in this response.** There is no combined RB/WR/TE ranking exposed. Producing a FLEX rank would require fetching the RB+WR+TE population together and re-ranking client-side (e.g. by projected points) — this adapter does **not** do this yet; it's a known, explicit gap, not a silent omission.

**Bottom line for D:** overall and per-position *current/preseason* ranking are proven and implemented. Weekly in-season ranking and any historical ranking are not — the adapter raises `ESPNSchemaError` rather than guessing when asked for either, which is intentional per the "fail loudly" requirement, not a bug to be silently patched over.

## E. Player population / no silent truncation

**Proven, and the default behavior would have been a silent-truncation trap.** With no `x-fantasy-filter` header at all, the endpoint returns only **50 players**, alphabetically by last name (Abanikanda, Abdullah, Achane, Adams, Addison, ... Bateman, Bates, Bates, Bates, Bech) — i.e. the unfiltered default is unusable for ranking-driven consumption and would silently omit the vast majority of relevant players if not caught.

Sending `x-fantasy-filter: {"players":{"limit":2000,"sortDraftRanks":{"sortPriority":1,"sortAsc":true,"value":"STANDARD"}}}` returned **1036 players**, sorted sensibly by draft value (Jahmyr Gibbs, Bijan Robinson, Ja'Marr Chase, Puka Nacua, ... down to deep-bench/practice-squad names). 1036 is comfortably under the requested `limit:2000`, i.e. this was not truncated by the cap — 1036 appears to be the full rosterable/rankable 2026 population for this context. The adapter defaults to `limit:3000` (headroom above the observed 1036) with the same sort, or `filterIds` when the caller wants specific players only.

Not independently re-verified: whether 1036 is stable across the season, or whether an even larger `limit` would surface more (deep/inactive) players. Treated as sufficient for QB/RB/WR/TE and overall ranking ingestion, not as an absolute ceiling. FLEX remains a tracker-derived capability if the project elects to implement it; it is not an ESPN-provided ranking, and the 1,036-player population test should not be read as proof that ESPN provides one — see item D.

## F. No personal authentication required

**Proven twice.** Every request in this investigation (population, rankings, weekly stats, for 4 different players across 2 seasons) succeeded with HTTP 200 using nothing but a plain `fetch()` and the `x-fantasy-filter` header. The final confirmation request explicitly used `credentials: 'omit'` (guarantees zero cookies sent, browser-enforced) and **no** `Authorization` header, and still returned complete data.

One incidental note, disclosed for transparency: the browser profile used for this research happened to already carry an ESPN-set `SWID` cookie (`SWID=DF73E1CA-...`) — this is a generic anonymous-visitor tracking cookie ESPN sets for any site visitor, not something requested or used by this research, and the `credentials:'omit'` test proves it plays no role in the endpoint working. Neither `espn_s2` nor any `Authorization` value was ever present, requested, or sent. `ESPNSourceAdapter` uses a plain `requests.Session()` with no cookies ever added and only the `x-fantasy-filter`/`Accept` headers — see the module docstring in `espn_adapter.py`.

## Notes on reverse-engineered docs vs. observed behavior

Per the brief's instruction to trust observed behavior over third-party docs: three commonly-cited filter keys (`filterStatsForSourceIds`, `filterRanksForScoringPeriodIds`, `filterRanksForRankTypes`) were tested directly against the live endpoint and had no observable effect at all — they are not relied on anywhere in this adapter. `filterStatsForTopScoringPeriodIds` does exist and does something, but not "select period X" — it silently drops projections, so it is also deliberately not used. Only `filterIds`, `sortDraftRanks`, and `limit` were confirmed to change server behavior.

## Environment note

This sandbox's own outbound network (`curl`/`requests` from the sandbox's bash shell) cannot reach `lm-api-reads.fantasy.espn.com` at all — blocked by the sandbox's egress proxy allowlist. Every live request made *during the research phase* was through the browser tool's in-page `fetch()`, not from this repo's own process. `ESPNSourceAdapter` (in `espn_adapter.py`) was subsequently run live, unmodified, from a normal Windows machine with real internet access (see the 2026-09-02 update at the top of this document) and confirmed correct — this caveat is resolved.

## Fixture provenance

`fixtures/qb_rb_wr_te_2025_2026_raw.json` — captured live this session via:
```js
fetch(url2025, {headers: {'x-fantasy-filter': JSON.stringify({players:{filterIds:{value:[3918298,4429795,4362628,4361307]}}})}})
fetch(url2026, {headers: {'x-fantasy-filter': JSON.stringify({players:{filterIds:{value:[3918298,4429795,4362628,4361307]}}})}})
```
for player ids 3918298 (Josh Allen, QB), 4429795 (Jahmyr Gibbs, RB), 4362628 (Ja'Marr Chase, WR), 4361307 (Trey McBride, TE) — one real, unambiguous, well-known player per required position, captured 2026-09-02.

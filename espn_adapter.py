"""
ESPNSourceAdapter: the live-HTTP implementation of SourceAdapter.

SECURITY: this module must never construct, accept, or forward
espn_s2, SWID, or any Authorization/cookie header. Every call in this
file uses a plain, cookie-less `requests` call — `requests` does not
send cookies unless you hand it a session/cookie jar that has some, and
this code never does that. This was also verified live in-browser with
`credentials: 'omit'` (see FINDINGS.md, section F): the endpoint returns
full data with zero cookies and zero auth headers sent.

Everything else in this file (URL shape, the X-Fantasy-Filter header,
which filter keys actually do something) is written to match what was
empirically observed this session — see FINDINGS.md for the evidence.
Do not add a new filter key here without first proving its effect the
same way (a real request, diffed against a request without it).
"""

from __future__ import annotations

import json
from typing import Optional

import requests

from espn_core import (
    ESPNSchemaError,
    parse_overall_ranking,
    parse_position_ranking,
    parse_weekly_actual,
    parse_weekly_projection,
)
from normalized import NormalizedObservation, SourceAdapter

BASE_URL_TMPL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season_id}"
    "/segments/0/leaguedefaults/3"
)

# PROVEN this session: the only request params/headers that changed
# server behavior were:
#   - the `view=kona_player_info` query param (required; this is what
#     makes the response include stats/rankings/outlook at all)
#   - the `X-Fantasy-Filter` request header, a JSON object, where:
#       players.limit                  -> caps population size (default
#                                          population with NO filter
#                                          header at all was just 50,
#                                          alphabetical by last name —
#                                          NOT usable as-is; see FINDINGS.md)
#       players.sortDraftRanks         -> {sortPriority, sortAsc, value}
#                                          sorts by draft rank; combined
#                                          with a large `limit` this is
#                                          how you get the full relevant
#                                          population (1036 players
#                                          returned with limit=2000,
#                                          i.e. it did not hit the cap)
#       players.filterIds              -> {value: [id, id, ...]} scopes
#                                          to specific player ids
#
# Filter keys from the brief that were TESTED and had ZERO observable
# effect on the response (kept here, disabled, as a record — do not
# silently re-enable without new proof):
#   filterStatsForSourceIds        (no change vs. omitting it)
#   filterRanksForScoringPeriodIds (no change vs. omitting it)
#   filterRanksForRankTypes        (no change vs. omitting it)
#
# filterStatsForTopScoringPeriodIds WAS observed to change the response,
# but not as its name suggests: {value: 5} returned only the 5 MOST
# RECENT statSourceId=0 (actual) periods, silently dropping all
# statSourceId=1 (projected) entries. Because this adapter needs
# projections, this filter is deliberately NEVER sent — stats are
# fetched unfiltered per player and the correct week/source is selected
# client-side in espn_core.py, which is slower per-request but correct
# and does not depend on trusting an ESPN filter we could not fully
# characterize.

DEFAULT_POPULATION_LIMIT = 3000  # observed full 2026 population was 1036; headroom kept deliberately large


class ESPNSourceAdapter(SourceAdapter):
    source_name = "espn"

    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 15.0):
        # A fresh, cookie-less session by default. If a caller passes
        # their own `session`, this class still never adds auth to it,
        # but callers are responsible for not handing in one that
        # already carries espn_s2/SWID cookies.
        self._session = session or requests.Session()
        self._timeout = timeout

    def _fetch_raw(self, season_id: int, player_ids: Optional[list[int]] = None) -> dict:
        url = BASE_URL_TMPL.format(season_id=season_id)
        params = {"view": "kona_player_info"}

        players_filter: dict = {}
        if player_ids:
            players_filter["filterIds"] = {"value": player_ids}
        else:
            players_filter["limit"] = DEFAULT_POPULATION_LIMIT
            players_filter["sortDraftRanks"] = {
                "sortPriority": 1,
                "sortAsc": True,
                "value": "STANDARD",
            }
        headers = {
            "x-fantasy-filter": json.dumps({"players": players_filter}),
            "Accept": "application/json",
        }

        resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        if "players" not in data:
            raise ESPNSchemaError(
                f"ESPN response for season={season_id} missing 'players' key; "
                f"got top-level keys={list(data.keys())}. Refusing to proceed — "
                "the response shape may have changed."
            )
        return data

    def fetch(
        self,
        season_id: int,
        week_number: int,
        player_ids: Optional[list[int]] = None,
        include_rankings: bool = True,
        ranking_week: int = 0,
    ) -> list[NormalizedObservation]:
        """
        player_ids: optional explicit ESPN player id scoping (recommended
            for targeted use — much smaller/faster response). If omitted,
            fetches the full population (see DEFAULT_POPULATION_LIMIT).
        ranking_week: which rankings[] period to read (0 = season/overall
            context; the only one confirmed populated pre-season — see
            FINDINGS.md). Pass week_number's value explicitly once
            in-season ranking population has been reverified live.
        """
        raw = self._fetch_raw(season_id, player_ids=player_ids)
        observations: list[NormalizedObservation] = []
        for entry in raw["players"]:
            # PROJECTION: backend v1 full-population survey (2026-09) found that at
            # full population scale, missing a weekly projection is the COMMON case,
            # not a corner case — 433/1090 players (~40%) lacked one for a single
            # (season, week), for entirely mundane reasons: a team's bye week (zero
            # stats[] entries at all for that period, for anyone on that team — not
            # D/ST-specific), or a player who simply wasn't on an active/relevant
            # roster that week. ESPN's unscoped population query surfaces its whole
            # draftable/historical player-id space, not just that week's rostered
            # players. The adapter's contract is therefore: absence is expected and
            # must not abort the whole batch — only a genuine anomaly should.
            # This does NOT weaken "never fabricate a value" (still true — a missing
            # player simply produces no PROJECTION observation, never a substituted
            # 0) and does NOT change how ambiguity is handled: >1 matching stats[]
            # entries still raises ESPNDataAnomalyError (not a subclass of
            # ESPNSchemaError — see espn_core.py), which is deliberately NOT caught
            # here and propagates to abort the whole fetch(), same as for ACTUAL.
            try:
                observations.append(parse_weekly_projection(entry, season_id, week_number))
            except ESPNSchemaError:
                pass  # expected absence (bye week / not rostered that week) — skip this player only
            try:
                observations.append(parse_weekly_actual(entry, season_id, week_number))
            except ESPNSchemaError:
                pass  # no actual result yet for this period (zero matching stats[] entries) —
                # expected absence, fine, not an error for ACTUAL specifically. NOTE: this only
                # catches ESPNSchemaError. A duplicate/anomalous actual entry raises
                # ESPNDataAnomalyError instead (see espn_core.py), which is deliberately NOT
                # caught here and will propagate and fail the whole fetch() — an anomaly must
                # never be silently treated the same as "not played yet."
            if include_rankings:
                try:
                    observations.append(parse_overall_ranking(entry, season_id, ranking_week))
                    observations.append(parse_position_ranking(entry, season_id, ranking_week))
                except ESPNSchemaError:
                    # Rankings are the least proven part of this adapter
                    # (see FINDINGS.md, section D) — don't let a ranking
                    # gap for one player kill projection ingestion for
                    # everyone else. Projection/actual failures above are
                    # NOT swallowed this way; they propagate.
                    pass
        return observations

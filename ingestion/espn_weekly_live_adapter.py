"""
Live weekly ESPN adapter -- Phase: weekly-live integration.

This is a NEW adapter alongside ESPNSeasonProjectionsAdapter, not a
replacement. ESPNSeasonProjectionsAdapter continues to own season-long
PDF-derived projections; this adapter owns weekly projections/actuals
from ESPN's live, unauthenticated kona_player_info endpoint. Neither
file imports the other.

Confidence note: this adapter's projection observations use 0.85
(matching ESPNSeasonProjectionsAdapter's PDF-derived confidence),
since a live JSON field read is at least as reliable as text-position
PDF parsing. Actuals use 0.95 -- a completed game's box-score-derived
figure, independently corroborated multiple times during the mapping
evidence pass (see espn_weekly_stat_map.py).
"""
from __future__ import annotations

from typing import List

import requests

from ingestion.collector import AdapterUnavailable, NormalizedObservation, SourceAdapter
from ingestion.espn_weekly_core import ESPNWeeklySchemaError, parse_player_entry_weekly

ESPN_ENDPOINT = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3?view=kona_player_info"
)

_PROJECTION_CONFIDENCE = 0.85
_ACTUAL_CONFIDENCE = 0.95
_REQUEST_TIMEOUT_SECONDS = 20

# Comfortably above the ~1036 rosterable players observed live in
# FINDINGS.md section E -- avoids the endpoint's silent 50-player
# default truncation without assuming a specific ceiling.
_DEFAULT_PLAYER_LIMIT = 3000


class ESPNWeeklyLiveAdapter(SourceAdapter):
    """Live HTTP weekly projections/actuals from ESPN's public fantasy
    endpoint. No auth required (extensively verified -- see
    espn_adapter/FINDINGS.md section F). Raises AdapterUnavailable for
    any network/HTTP/schema failure, per this app's SourceAdapter
    contract -- never returns a partial silent result.

    IMPORTANT: fetch()'s season_id argument, per this app's
    SourceAdapter contract, is the database's internal seasons.season_id
    primary key (e.g. 1), NOT the real NFL season year ESPN's URL needs
    (e.g. 2026) -- confirmed the hard way: passing the DB's internal id
    straight into the ESPN URL produced a real 404 (".../seasons/1/...").
    NormalizedObservation carries no season field for the pipeline to
    resolve this from, so this adapter must be told the real year
    explicitly at construction time via season_year, independent of
    whatever season_id fetch() is called with. fetch()'s season_id
    argument is still accepted (required by the interface) but is not
    used to build the URL."""

    def __init__(self, source_name: str = "ESPN", season_year: int | None = None,
                 player_ids: list[int] | None = None, limit: int = _DEFAULT_PLAYER_LIMIT):
        self.source_name = source_name
        self.season_year = season_year
        self.player_ids = player_ids
        self.limit = limit
        self._session = requests.Session()

    def fetch(self, season_id: int, week_number: int) -> List[NormalizedObservation]:
        # season_id (the DB's internal seasons.season_id) is intentionally
        # NOT used here -- see class docstring. self.season_year (the real
        # NFL year) is what ESPN's URL needs; season_year falls back to
        # season_id only if it was never set, purely so older direct-call
        # usage (passing a literal year straight into fetch(), as the
        # manual live-check script does) keeps working unchanged.
        year = self.season_year if self.season_year is not None else season_id
        raw_entries = self._fetch_raw(year)

        observations: List[NormalizedObservation] = []
        for entry in raw_entries:
            try:
                observations.extend(
                    parse_player_entry_weekly(entry, year, week_number, _PROJECTION_CONFIDENCE)
                )
            except ESPNWeeklySchemaError:
                # One malformed player entry doesn't invalidate the whole
                # batch -- same absence-vs-anomaly principle established
                # for the standalone adapter's full-population run. A
                # systemically malformed response still surfaces because
                # _fetch_raw's own JSON/shape checks raise AdapterUnavailable
                # before we ever get here.
                continue

        # Actuals get their own confidence level; parse_player_entry_weekly
        # tags them by data_type already, so re-stamp confidence here rather
        # than threading a second confidence value through every call site.
        for obs in observations:
            if obs.data_type == "actual":
                obs.confidence = _ACTUAL_CONFIDENCE

        return observations

    def _fetch_raw(self, season_id: int) -> list[dict]:
        url = ESPN_ENDPOINT.format(season=season_id)
        filter_body = {"players": {"limit": self.limit,
                                    "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "STANDARD"}}}
        if self.player_ids:
            filter_body = {"players": {"filterIds": {"value": self.player_ids}}}

        import json as _json
        headers = {"x-fantasy-filter": _json.dumps(filter_body), "Accept": "application/json"}

        try:
            resp = self._session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as e:
            raise AdapterUnavailable(f"ESPN request timed out after {_REQUEST_TIMEOUT_SECONDS}s", transient=True) from e
        except requests.exceptions.ConnectionError as e:
            raise AdapterUnavailable(f"Could not connect to ESPN: {e}", transient=True) from e

        if resp.status_code == 403:
            raise AdapterUnavailable(f"ESPN returned 403 Forbidden (blocked or rate-limited): {resp.text[:200]}",
                                      transient=True)
        if resp.status_code >= 500:
            raise AdapterUnavailable(f"ESPN returned server error {resp.status_code}", transient=True)
        if resp.status_code != 200:
            raise AdapterUnavailable(f"ESPN returned unexpected status {resp.status_code}: {resp.text[:200]}",
                                      transient=False)

        try:
            data = resp.json()
        except ValueError as e:
            raise AdapterUnavailable(f"ESPN response was not valid JSON: {e}", transient=False) from e

        players = data.get("players")
        if players is None:
            raise AdapterUnavailable(
                f"ESPN response missing 'players' array entirely -- schema may have changed: "
                f"top-level keys={list(data.keys())}",
                transient=False,
            )
        return players

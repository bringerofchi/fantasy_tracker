"""
LocalFileSourceAdapter: reads a previously-captured raw ESPN response
(same wire shape as the live endpoint — a dict with a "players" list)
from a JSON file on disk and runs it through the same espn_core parsing
used by the live adapter.

This is what the test suite uses, so tests are 100% reproducible and
never depend on network access or on ESPN's live data changing under us.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from espn_core import (
    ESPNSchemaError,
    parse_overall_ranking,
    parse_position_ranking,
    parse_weekly_actual,
    parse_weekly_projection,
)
from normalized import NormalizedObservation, SourceAdapter


class LocalFileSourceAdapter(SourceAdapter):
    source_name = "espn_fixture"

    def __init__(self, path: str | Path, include_rankings: bool = True, ranking_week: Optional[int] = None):
        """
        path: JSON file containing a raw ESPN leaguedefaults response,
              i.e. {"players": [...]}. Also accepts the two-season
              fixture shape used in this project's fixtures/ directory
              ({"season2025": {"players": [...]}, "season2026": {...}})
              — pass season_id to fetch() matching one of those keys'
              season and this adapter will pick the right block.
        include_rankings: whether to also emit RANKING observations.
              Set False for a pure-projection fixture (e.g. a completed
              season, where ESPN does not retain rankings — see
              FINDINGS.md) to avoid raising on missing rankings.
        ranking_week: which rankings[] period key to use (0 = season
              context). Defaults to 0, the only period confirmed
              populated pre-season.
        """
        self.path = Path(path)
        self._raw = json.loads(self.path.read_text())
        self.include_rankings = include_rankings
        self.ranking_week = ranking_week if ranking_week is not None else 0

    def _players_for_season(self, season_id: int) -> list[dict]:
        if "players" in self._raw:
            return self._raw["players"]
        # multi-season fixture shape: {"season2025": {...}, "season2026": {...}}
        key = f"season{season_id}"
        if key in self._raw:
            return self._raw[key]["players"]
        raise ESPNSchemaError(
            f"fixture {self.path} has neither a top-level 'players' key nor a '{key}' "
            f"block; top-level keys are {list(self._raw.keys())}"
        )

    def fetch(self, season_id: int, week_number: int) -> list[NormalizedObservation]:
        observations: list[NormalizedObservation] = []
        for entry in self._players_for_season(season_id):
            observations.append(parse_weekly_projection(entry, season_id, week_number))
            try:
                observations.append(parse_weekly_actual(entry, season_id, week_number))
            except ESPNSchemaError:
                # Actual results legitimately don't exist yet for a
                # future/current week — that's fine, projections still
                # got added above. We only swallow this for ACTUAL,
                # never for PROJECTION. A duplicate/anomalous actual
                # entry raises ESPNDataAnomalyError instead (not a
                # subclass of ESPNSchemaError — see espn_core.py) and is
                # deliberately NOT caught here, so it propagates.
                pass
            if self.include_rankings:
                observations.append(parse_overall_ranking(entry, season_id, self.ranking_week))
                observations.append(parse_position_ranking(entry, season_id, self.ranking_week))
        return observations

"""
Source-independent normalized data model for the NFL Fantasy Data Tracker.

This module intentionally contains NOTHING ESPN-specific. Any adapter
(ESPN, or a future source) must translate its own wire format into
these types. See espn_adapter.py for the ESPN translation layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Position(str, Enum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "D/ST"


class DataType(str, Enum):
    PROJECTION = "projection"       # a single week's projected fantasy points
    ACTUAL = "actual"                # a single week's realized fantasy points
    RANKING = "ranking"              # a positional/overall rank for a period
    SEASON_PROJECTION = "season_projection"  # season-long (not weekly) projection


class RankingType(str, Enum):
    OVERALL = "overall"
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    FLEX = "FLEX"


@dataclass(frozen=True)
class PlayerIdentity:
    source_player_id: str      # the source's native player id (ESPN: player.id), as a string
    full_name: str
    position: Position
    pro_team_id: Optional[int] = None   # source-native team id; adapters may also resolve an abbreviation
    pro_team_abbrev: Optional[str] = None


@dataclass(frozen=True)
class NormalizedObservation:
    """
    One fact about one player for one (season, week) — a projection, an
    actual result, or a ranking. Never a bundle of several facts.
    """
    source: str                      # e.g. "espn"
    season_id: int
    week_number: int                 # 0 = season-long / non-weekly observation
    data_type: DataType
    player: PlayerIdentity

    # populated for data_type in {PROJECTION, ACTUAL, SEASON_PROJECTION}
    fantasy_points: Optional[float] = None
    scoring_format: Optional[str] = None   # e.g. "PPR"

    # populated for data_type == RANKING
    ranking_type: Optional[RankingType] = None
    rank: Optional[float] = None           # float because ESPN's consensus rank can be fractional (averageRank)

    # provenance / auditability
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_record_id: Optional[str] = None   # e.g. the raw ESPN stats[].id, for traceability
    raw: Optional[dict] = None               # optional: the untouched source fragment this was derived from


class SourceAdapter(ABC):
    """
    Common interface every data source (ESPN, others later) must implement.
    Adapters own all source-specific request/response handling; nothing
    ESPN-specific (field names, filter syntax, quirks) should leak past
    this boundary.
    """

    #: short machine-readable name, e.g. "espn"
    source_name: str = "unknown"

    @abstractmethod
    def fetch(self, season_id: int, week_number: int) -> list[NormalizedObservation]:
        """
        Return every NormalizedObservation this adapter can produce for the
        given season/week (projections, rankings, and actuals where
        available). Must raise rather than silently return an empty or
        partial result if the source's response doesn't match the shape
        the adapter expects.
        """
        raise NotImplementedError

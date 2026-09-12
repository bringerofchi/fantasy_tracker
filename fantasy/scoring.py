"""
Single authoritative fantasy scoring engine.

Every place in the app that needs a fantasy point total (dashboard, player
weekly view, trend view, accuracy summary, CSV export) must go through
calculate_fantasy_points() with a scoring config from this module. No
formula is duplicated anywhere else.

Design notes:
- Scoring config is a plain dict so it's trivial to add HALF_PPR / STANDARD
  configs later without touching the calculation function (spec sec. 2, 4).
- calculate_fantasy_points operates on raw stat values and never sees a
  database connection — it is a pure function, easy to unit test.
- A stat with value None is treated as MISSING, not zero. Missing stats
  contribute 0 to the total (so a score can still be shown) but are
  reported separately via `missing` / `is_complete` so callers can warn
  the user rather than presenting an incomplete total as if it were final
  (spec sec. 5, 9).
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# The current application's scoring configuration: PPR (spec sec. 2)
PPR_SCORING = {
    "passing_yard_points": 0.04,
    "passing_td_points": 4,
    "interception_points": -2,
    "rushing_yard_points": 0.10,
    "rushing_td_points": 6,
    "reception_points": 1,
    "receiving_yard_points": 0.10,
    "receiving_td_points": 6,
    "fumble_lost_points": -2,
}

# Maps a raw stat_name (as stored in stat_definitions/statistics/projections)
# to the scoring-config key and its point value. A stat_name with no entry
# here (e.g. 'completions_attempts') is informational only and never scored,
# so it can never "accidentally become scoring points" (spec sec. 3).
STAT_TO_SCORING_KEY = {
    "pass_yards": "passing_yard_points",
    "pass_tds": "passing_td_points",
    "interceptions": "interception_points",
    "rush_yards": "rushing_yard_points",
    "rush_tds": "rushing_td_points",
    "receptions": "reception_points",
    "receiving_yards": "receiving_yard_points",
    "receiving_tds": "receiving_td_points",
    "fumbles_lost": "fumble_lost_points",
}


@dataclass
class FantasyPointsResult:
    total_points: float
    breakdown: Dict[str, float] = field(default_factory=dict)  # stat_name -> points contributed
    missing: List[str] = field(default_factory=list)            # scorable stat_names with no value
    is_complete: bool = True


def calculate_fantasy_points(stat_values: Dict[str, Optional[float]],
                              scoring_config: Dict[str, float] = PPR_SCORING) -> FantasyPointsResult:
    """
    stat_values: dict of stat_name -> numeric value, or None if that stat is
    missing/not collected for this player/week. Only keys present in
    STAT_TO_SCORING_KEY are scored; anything else (e.g. completions_attempts,
    or a stat that legitimately doesn't apply to this position, such as a
    WR's pass_yards) is ignored rather than scored as zero.

    Returns a FantasyPointsResult. Missing stats contribute 0 to total_points
    but are listed in `missing` and flip `is_complete` to False so the caller
    can display an "incomplete" indicator instead of presenting a partial
    total as final.
    """
    total = 0.0
    breakdown: Dict[str, float] = {}
    missing: List[str] = []

    for stat_name, value in stat_values.items():
        scoring_key = STAT_TO_SCORING_KEY.get(stat_name)
        if scoring_key is None:
            continue  # not a scorable stat for this engine (e.g. completions_attempts)
        if value is None:
            missing.append(stat_name)
            continue
        points = value * scoring_config[scoring_key]
        breakdown[stat_name] = round(points, 3)
        total += points

    return FantasyPointsResult(
        total_points=round(total, 2),
        breakdown=breakdown,
        missing=missing,
        is_complete=(len(missing) == 0),
    )

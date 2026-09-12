"""
ESPN-specific parsing logic: translates one raw ESPN "player entry" (an
element of the leaguedefaults `players[]` array, view=kona_player_info)
into NormalizedObservation records.

Every fact asserted in a docstring/comment here was verified this
session against real, live, unauthenticated HTTP responses from
    https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/3?view=kona_player_info
using the browser's fetch() with credentials:'omit' and no Authorization
header (see FINDINGS.md for the full research log, including the things
that did NOT work). Nothing here should be taken as "how ESPN's API
generally works" beyond what FINDINGS.md documents as proven.

This module is deliberately the ONLY place ESPN's field names/quirks
are allowed to appear. Both ESPNSourceAdapter (live HTTP) and
LocalFileSourceAdapter (fixture files, same wire shape) call into it,
so a single, tested parser backs both.
"""

from __future__ import annotations

from typing import Any, Iterable

from espn_teams import PRO_TEAM_ID_TO_ABBREV
from normalized import (
    DataType,
    NormalizedObservation,
    PlayerIdentity,
    Position,
    RankingType,
)

# --- Position mapping -------------------------------------------------
# defaultPositionId -> Position
# CONFIDENCE: 1=QB, 2=RB, 3=WR, 4=TE were independently confirmed this
# session (Josh Allen=1, Jahmyr Gibbs=2, Ja'Marr Chase=3, Trey McBride=4
# in a live response). 5=K and 16=D/ST are the standard published ESPN
# convention, NOT independently re-verified this session.
DEFAULT_POSITION_ID_TO_POSITION: dict[int, Position] = {
    1: Position.QB,
    2: Position.RB,
    3: Position.WR,
    4: Position.TE,
    5: Position.K,
    16: Position.DST,
}

# statSourceId observed values (proven this session on 2025 historical
# data for a QB, RB, WR, and TE, week 5): every scoring period that has
# both an actual game and a projection carries TWO stats[] entries with
# identical scoringPeriodId/seasonId/statSplitTypeId but different
# statSourceId:
#   0 -> ACTUAL   (id pattern "0" + externalId, externalId = real NFL game id, e.g. "01401772854")
#   1 -> PROJECTED (id pattern "1" + seasonId + scoringPeriodId, e.g. "1120255" = 1+2025+5)
STAT_SOURCE_ACTUAL = 0
STAT_SOURCE_PROJECTED = 1

# statSplitTypeId observed values:
#   0 -> season aggregate (paired with scoringPeriodId == 0)
#   1 -> single scoring period (weekly)
#   2 -> observed once, alongside a per-game "average" style total at
#        scoringPeriodId 0; NOT fully characterized this session, and
#        not used by this parser. Flagged in FINDINGS.md as unresolved.
STAT_SPLIT_SEASON_TOTAL = 0
STAT_SPLIT_WEEKLY = 1


class ESPNSchemaError(RuntimeError):
    """
    Raised when a raw ESPN player entry doesn't match the shape this
    parser was built and validated against. This is intentional: the
    project's requirement is to fail loudly rather than silently
    substitute a default, a season total for a weekly figure, or a
    current ranking for a historical one.
    """


def _get_player_dict(entry: dict[str, Any]) -> dict[str, Any]:
    player = entry.get("player")
    if not isinstance(player, dict):
        raise ESPNSchemaError(f"player entry missing 'player' object: keys={list(entry.keys())}")
    return player


def parse_identity(entry: dict[str, Any]) -> PlayerIdentity:
    player = _get_player_dict(entry)
    try:
        source_player_id = str(player["id"])
        full_name = player["fullName"]
        default_position_id = player["defaultPositionId"]
    except KeyError as e:
        raise ESPNSchemaError(f"player entry missing required identity field {e}") from e

    position = DEFAULT_POSITION_ID_TO_POSITION.get(default_position_id)
    if position is None:
        raise ESPNSchemaError(
            f"unrecognized defaultPositionId={default_position_id} for player "
            f"{full_name!r} (id={source_player_id}); refusing to guess a position"
        )

    pro_team_id = player.get("proTeamId")
    pro_team_abbrev = PRO_TEAM_ID_TO_ABBREV.get(pro_team_id) if pro_team_id is not None else None

    return PlayerIdentity(
        source_player_id=source_player_id,
        full_name=full_name,
        position=position,
        pro_team_id=pro_team_id,
        pro_team_abbrev=pro_team_abbrev,
    )


def _iter_stats(entry: dict[str, Any]) -> Iterable[dict[str, Any]]:
    player = _get_player_dict(entry)
    stats = player.get("stats")
    if stats is None:
        raise ESPNSchemaError(
            f"player entry for {player.get('fullName')!r} has no 'stats' array at all "
            "(expected at least an empty list); refusing to treat missing data as zero"
        )
    return stats


def find_weekly_stat_entry(
    entry: dict[str, Any],
    season_id: int,
    week_number: int,
    stat_source_id: int,
) -> dict[str, Any]:
    """
    Return the single stats[] entry matching
    (seasonId, scoringPeriodId=week_number, statSourceId, statSplitTypeId=WEEKLY).

    Raises ESPNSchemaError if there isn't EXACTLY one match — ambiguity
    or absence is a hard error here, never silently coerced to 0.
    """
    if week_number <= 0:
        raise ValueError("week_number must be a positive NFL week (1-18); use season-total helpers for week 0")

    matches = [
        s
        for s in _iter_stats(entry)
        if s.get("seasonId") == season_id
        and s.get("scoringPeriodId") == week_number
        and s.get("statSourceId") == stat_source_id
        and s.get("statSplitTypeId") == STAT_SPLIT_WEEKLY
    ]
    player = _get_player_dict(entry)
    if len(matches) == 0:
        raise ESPNSchemaError(
            f"no weekly stats[] entry for {player.get('fullName')!r} "
            f"season={season_id} week={week_number} statSourceId={stat_source_id}; "
            "the source may not have this period's data yet (e.g. future week) — "
            "do not substitute 0 or a season total"
        )
    if len(matches) > 1:
        raise ESPNSchemaError(
            f"expected exactly one weekly stats[] entry for {player.get('fullName')!r} "
            f"season={season_id} week={week_number} statSourceId={stat_source_id}, "
            f"found {len(matches)}: {matches}"
        )
    return matches[0]


def parse_weekly_projection(entry: dict[str, Any], season_id: int, week_number: int) -> NormalizedObservation:
    identity = parse_identity(entry)
    stat = find_weekly_stat_entry(entry, season_id, week_number, STAT_SOURCE_PROJECTED)
    if "appliedTotal" not in stat:
        raise ESPNSchemaError(f"projected stats entry for {identity.full_name} missing 'appliedTotal': {stat}")
    return NormalizedObservation(
        source="espn",
        season_id=season_id,
        week_number=week_number,
        data_type=DataType.PROJECTION,
        player=identity,
        fantasy_points=float(stat["appliedTotal"]),
        scoring_format="PPR",
        source_record_id=str(stat.get("id")),
        raw=stat,
    )


def parse_weekly_actual(entry: dict[str, Any], season_id: int, week_number: int) -> NormalizedObservation:
    identity = parse_identity(entry)
    stat = find_weekly_stat_entry(entry, season_id, week_number, STAT_SOURCE_ACTUAL)
    if "appliedTotal" not in stat:
        raise ESPNSchemaError(f"actual stats entry for {identity.full_name} missing 'appliedTotal': {stat}")
    return NormalizedObservation(
        source="espn",
        season_id=season_id,
        week_number=week_number,
        data_type=DataType.ACTUAL,
        player=identity,
        fantasy_points=float(stat["appliedTotal"]),
        scoring_format="PPR",
        source_record_id=str(stat.get("id")),
        raw=stat,
    )


# --- Rankings -----------------------------------------------------------
# PROVEN this session (see FINDINGS.md section D):
#   * entry['player']['rankings'] is a dict keyed by scoringPeriodId-as-string
#     ("0" = season/overall context, "1".."N" = that NFL week's context).
#   * within each period's list, the element with rankSourceId == 0 is
#     ESPN's own aggregate/consensus entry (as opposed to rankSourceId
#     3,5,6,7,... which are individual outside expert sources ESPN blends
#     into that consensus).
#   * the consensus entry's `rank` field is frequently 0 (a sentinel) —
#     the real fractional consensus rank is in `averageRank`.
#   * the consensus entry's `slotId` matches the PLAYER'S OWN natural
#     position slot (0=QB,2=RB,4=WR,6=TE) — i.e. rankings[...] gives a
#     POSITION-scoped rank (this player's rank among others at their own
#     position), not an overall rank.
#   * the TRUE overall (cross-position) consensus rank lives in a
#     DIFFERENT field: player['draftRanksByRankType']['PPR']['rank'].
#   * NOT proven / explicitly flagged as unresolved:
#       - whether rankings[str(week_number)] (week_number > 0) is
#         populated with a real consensus once the season is underway.
#         Before 2026 week 1, it was observed present but sparse (no
#         rankSourceId==0 consensus entry at all for week 1 — see
#         FINDINGS.md). This must be re-checked live once games start.
#       - rankings for a SEASON THAT HAS ALREADY ENDED: for the 2025
#         season (queried in Sept 2026), `rankings` came back as null/
#         absent (not even an empty dict) for every player tested,
#         regardless of filters. ESPN does not appear to retain
#         historical weekly ranking snapshots via this endpoint. A
#         historical "what was the PPR rank in week 5 2025" query
#         cannot be answered from this field.
#         By contrast, `draftRanksByRankType` (used by
#         parse_overall_ranking below) WAS still populated for the 2025
#         season even when queried after the season ended — but it reads
#         as the PRESEASON/draft-time consensus for that season (e.g.
#         Ja'Marr Chase showed rank=1 for 2025, which is a preseason
#         value, not a reflection of how 2025 actually played out).
#         Treat parse_overall_ranking's output as "preseason consensus
#         rank for that season," not a retrospective performance rank,
#         for any already-completed season.
#   * There is no FLEX-specific field anywhere in the response. A FLEX
#     ranking (RB/WR/TE ranked together) is NOT an ESPN-provided value
#     here; it would have to be derived by the caller by pooling
#     RB+WR+TE players and re-ranking by projected points or overall
#     rank. This adapter does not fabricate one; see FINDINGS.md.

RANK_SOURCE_CONSENSUS = 0
RANK_TYPE_PPR = "PPR"


def parse_position_ranking(entry: dict[str, Any], season_id: int, week_number: int) -> NormalizedObservation:
    """
    Position-scoped PPR consensus rank (ranking_type = the player's own
    position: QB/RB/WR/TE) for the given scoring period.

    week_number == 0 means the season/overall context (rankings["0"]),
    which is the only context confirmed populated pre-season. Passing a
    week_number > 0 uses rankings[str(week_number)] and will raise if
    that period has no consensus (rankSourceId==0) entry yet — this is
    expected/current behavior pre-season per FINDINGS.md, not a bug.
    """
    identity = parse_identity(entry)
    player = _get_player_dict(entry)
    rankings = player.get("rankings")
    if not isinstance(rankings, dict):
        raise ESPNSchemaError(f"player entry for {identity.full_name} has no 'rankings' object")

    period_key = str(week_number)
    period_list = rankings.get(period_key)
    if not period_list:
        raise ESPNSchemaError(
            f"no rankings['{period_key}'] data for {identity.full_name}; "
            "ESPN does not retain this for completed seasons, and per-week "
            "consensus may not yet be published for a future week — see FINDINGS.md"
        )

    consensus = [
        r for r in period_list
        if r.get("rankSourceId") == RANK_SOURCE_CONSENSUS and r.get("rankType") == RANK_TYPE_PPR
    ]
    if len(consensus) != 1:
        raise ESPNSchemaError(
            f"expected exactly one PPR consensus ranking entry (rankSourceId=0) for "
            f"{identity.full_name} period={period_key}, found {len(consensus)}: {consensus}"
        )
    r = consensus[0]
    rank_value = r.get("averageRank")
    if rank_value is None:
        rank_value = r.get("rank")
    if rank_value is None:
        raise ESPNSchemaError(f"consensus ranking entry for {identity.full_name} has neither averageRank nor rank: {r}")

    ranking_type = {
        Position.QB: RankingType.QB,
        Position.RB: RankingType.RB,
        Position.WR: RankingType.WR,
        Position.TE: RankingType.TE,
    }.get(identity.position)
    if ranking_type is None:
        raise ESPNSchemaError(f"no RankingType mapping for position {identity.position} ({identity.full_name})")

    return NormalizedObservation(
        source="espn",
        season_id=season_id,
        week_number=week_number,
        data_type=DataType.RANKING,
        player=identity,
        ranking_type=ranking_type,
        rank=float(rank_value),
        scoring_format="PPR",
        source_record_id=None,
        raw=r,
    )


def parse_overall_ranking(entry: dict[str, Any], season_id: int, week_number: int) -> NormalizedObservation:
    """
    Cross-position overall PPR consensus rank, from
    player['draftRanksByRankType']['PPR']['rank']. This field is NOT
    keyed by scoring period in the response (it reflects "current"
    draft-style consensus); week_number is carried through purely as
    the caller's requested context and is not itself used to select
    a different value. See FINDINGS.md for what was and wasn't proven
    about how this updates in-season.
    """
    identity = parse_identity(entry)
    player = _get_player_dict(entry)
    draft_ranks = player.get("draftRanksByRankType")
    if not isinstance(draft_ranks, dict):
        raise ESPNSchemaError(f"player entry for {identity.full_name} has no 'draftRanksByRankType' object")
    ppr = draft_ranks.get("PPR")
    if not isinstance(ppr, dict) or "rank" not in ppr:
        raise ESPNSchemaError(f"draftRanksByRankType.PPR missing/incomplete for {identity.full_name}: {ppr}")

    return NormalizedObservation(
        source="espn",
        season_id=season_id,
        week_number=week_number,
        data_type=DataType.RANKING,
        player=identity,
        ranking_type=RankingType.OVERALL,
        rank=float(ppr["rank"]),
        scoring_format="PPR",
        source_record_id=None,
        raw=ppr,
    )

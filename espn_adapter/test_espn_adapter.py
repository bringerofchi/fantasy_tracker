"""
Regression tests against the real, captured 2025/2026 fixture
(fixtures/qb_rb_wr_te_2025_2026_raw.json — four real players, one per
required position, pulled live from ESPN's public endpoint this
session; see FINDINGS.md for exact provenance and the request that
produced it).

Run with:  python3 -m pytest test_espn_adapter.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from espn_core import ESPNSchemaError
from local_file_adapter import LocalFileSourceAdapter
from normalized import DataType, Position, RankingType

FIXTURE = Path(__file__).parent / "fixtures" / "qb_rb_wr_te_2025_2026_raw.json"

# Ground truth captured live this session (see FINDINGS.md section C).
# name -> (espn player id, position, week5_2025_projected, week5_2025_actual)
KNOWN_2025_WEEK5 = {
    "Josh Allen": (3918298, Position.QB, 22.29365337, 19.42),
    "Jahmyr Gibbs": (4429795, Position.RB, 22.06789034, 16.7),
    "Ja'Marr Chase": (4362628, Position.WR, 16.05840835, 29.0),
    "Trey McBride": (4361307, Position.TE, 15.46032864, 9.1),
}


@pytest.fixture
def adapter_2025():
    # rankings{} is not retained by ESPN for a completed season (proven
    # this session — see FINDINGS.md section D), so fetch() must run
    # with include_rankings=False here or it will raise inside the
    # position-ranking step for every player. The still-present
    # draftRanksByRankType (overall rank) path is exercised directly in
    # TestRankings.test_2025_completed_season_still_has_overall_draft_rank
    # instead of via fetch().
    return LocalFileSourceAdapter(FIXTURE, include_rankings=False)


@pytest.fixture
def adapter_2026():
    return LocalFileSourceAdapter(FIXTURE, include_rankings=True, ranking_week=0)


def _by(observations, data_type, name=None):
    out = [o for o in observations if o.data_type == data_type]
    if name is not None:
        out = [o for o in out if o.player.full_name == name]
    return out


class TestWeeklyProjectionKnownPlayers:
    """Item C / testing requirement: one known QB, RB, WR, TE, week 5 2025."""

    @pytest.mark.parametrize("name", list(KNOWN_2025_WEEK5))
    def test_projection_matches_known_value(self, adapter_2025, name):
        espn_id, position, expected_proj, expected_actual = KNOWN_2025_WEEK5[name]
        obs = adapter_2025.fetch(season_id=2025, week_number=5)

        proj = _by(obs, DataType.PROJECTION, name)
        assert len(proj) == 1, f"expected exactly one projection for {name}, got {len(proj)}"
        p = proj[0]
        assert p.player.source_player_id == str(espn_id)
        assert p.player.position == position
        assert p.week_number == 5
        assert p.season_id == 2025
        assert p.fantasy_points == pytest.approx(expected_proj)
        assert p.scoring_format == "PPR"

    @pytest.mark.parametrize("name", list(KNOWN_2025_WEEK5))
    def test_actual_matches_known_value(self, adapter_2025, name):
        espn_id, position, expected_proj, expected_actual = KNOWN_2025_WEEK5[name]
        obs = adapter_2025.fetch(season_id=2025, week_number=5)

        actual = _by(obs, DataType.ACTUAL, name)
        assert len(actual) == 1
        a = actual[0]
        assert a.fantasy_points == pytest.approx(expected_actual)

    def test_projection_and_actual_are_never_equal_when_both_present(self, adapter_2025):
        # Sanity guard against the exact bug the brief warns about:
        # silently substituting one for the other. For every one of our
        # four known players' week 5, projected != actual in the raw
        # data, so if the adapter ever collapses them to the same value
        # something has gone wrong upstream in parsing.
        obs = adapter_2025.fetch(season_id=2025, week_number=5)
        for name in KNOWN_2025_WEEK5:
            proj = _by(obs, DataType.PROJECTION, name)[0].fantasy_points
            actual = _by(obs, DataType.ACTUAL, name)[0].fantasy_points
            assert proj != actual


class TestWeekNumberChangesResult:
    """Testing requirement: changing week_number changes the requested period."""

    def test_week5_vs_week12_differ(self, adapter_2025):
        obs5 = adapter_2025.fetch(season_id=2025, week_number=5)
        obs12 = adapter_2025.fetch(season_id=2025, week_number=12)

        gibbs5 = _by(obs5, DataType.PROJECTION, "Jahmyr Gibbs")[0]
        gibbs12 = _by(obs12, DataType.PROJECTION, "Jahmyr Gibbs")[0]

        assert gibbs5.week_number == 5
        assert gibbs12.week_number == 12
        assert gibbs5.fantasy_points != gibbs12.fantasy_points
        # and both should be the (distinct) known-good values from the
        # raw fixture, not e.g. the same season-total figure reused
        assert gibbs5.fantasy_points == pytest.approx(22.06789034)
        assert gibbs12.fantasy_points == pytest.approx(21.02343159)

    def test_week_number_must_be_positive(self, adapter_2025):
        with pytest.raises(ValueError):
            adapter_2025.fetch(season_id=2025, week_number=0)


class TestMissingDataFailsLoudly:
    """Do not treat missing weekly data as zero (explicit brief requirement)."""

    def test_future_week_with_no_projection_raises(self, adapter_2025):
        # Week 25 does not exist in the 18-week-season fixture data at all.
        with pytest.raises(ESPNSchemaError):
            adapter_2025.fetch(season_id=2025, week_number=25)

    def test_unknown_season_raises(self, adapter_2025):
        with pytest.raises(ESPNSchemaError):
            adapter_2025.fetch(season_id=1999, week_number=5)


class TestRankings:
    """
    Item D. Deliberately asymmetric: 2026 (current/preseason context) has
    populated rankings; 2025 (completed season) does not retain them —
    see FINDINGS.md. Both behaviors are asserted so a future change in
    either direction is caught rather than silently accepted.
    """

    def test_2026_overall_and_position_rankings_present(self, adapter_2026):
        obs = adapter_2026.fetch(season_id=2026, week_number=1)
        overall = _by(obs, DataType.RANKING, "Jahmyr Gibbs")
        overall = [o for o in overall if o.ranking_type == RankingType.OVERALL]
        position = [
            o for o in _by(obs, DataType.RANKING, "Jahmyr Gibbs")
            if o.ranking_type == RankingType.RB
        ]
        assert len(overall) == 1
        assert len(position) == 1
        assert overall[0].rank == pytest.approx(1.0)   # Gibbs: #1 overall PPR consensus, preseason 2026
        assert position[0].rank == pytest.approx(1.25)  # Gibbs: #1-ish RB-specific consensus, preseason 2026

    def test_2026_position_ranking_is_position_scoped_not_overall(self, adapter_2026):
        # Josh Allen is a top-tier QB but nowhere near the #1 OVERALL
        # PPR pick (running backs/WRs dominate overall). His QB-position
        # rank should be much better (lower) than his overall rank,
        # proving these two fields are genuinely different scopes and
        # not accidentally reading the same underlying number twice.
        obs = adapter_2026.fetch(season_id=2026, week_number=1)
        allen = _by(obs, DataType.RANKING, "Josh Allen")
        overall = [o for o in allen if o.ranking_type == RankingType.OVERALL][0]
        qb_rank = [o for o in allen if o.ranking_type == RankingType.QB][0]
        assert qb_rank.rank < overall.rank

    def test_2025_completed_season_has_no_position_rankings(self):
        # rankings{} is absent for a completed season — must raise, not
        # silently return an empty/zero rank.
        adapter = LocalFileSourceAdapter(FIXTURE, include_rankings=True, ranking_week=0)
        with pytest.raises(ESPNSchemaError):
            adapter.fetch(season_id=2025, week_number=1)  # raises when it hits parse_position_ranking

    def test_2025_completed_season_still_has_overall_draft_rank(self):
        # draftRanksByRankType persists after a season ends (it's a
        # preseason snapshot, not wiped) — exercised directly rather
        # than via fetch() since fetch() raises on the position-ranking
        # step above for this fixture.
        from espn_core import parse_overall_ranking
        import json

        raw = json.loads(FIXTURE.read_text())
        chase_entry = next(
            p for p in raw["season2025"]["players"] if p["player"]["fullName"] == "Ja'Marr Chase"
        )
        obs = parse_overall_ranking(chase_entry, season_id=2025, week_number=0)
        assert obs.rank == pytest.approx(1.0)  # Chase's PRESEASON 2025 overall PPR consensus rank


class TestIdentity:
    def test_positions_match_known_players(self, adapter_2025):
        obs = adapter_2025.fetch(season_id=2025, week_number=5)
        for name, (espn_id, position, _, _) in KNOWN_2025_WEEK5.items():
            proj = _by(obs, DataType.PROJECTION, name)[0]
            assert proj.player.position == position
            assert proj.player.source_player_id == str(espn_id)

    def test_pro_team_abbrev_matches_known_teams(self, adapter_2025):
        # Confirmed live this session: Allen=BUF, Chase=CIN, Gibbs=DET, McBride=ARI
        expected = {
            "Josh Allen": "BUF",
            "Jahmyr Gibbs": "DET",
            "Ja'Marr Chase": "CIN",
            "Trey McBride": "ARI",
        }
        obs = adapter_2025.fetch(season_id=2025, week_number=5)
        for name, team in expected.items():
            proj = _by(obs, DataType.PROJECTION, name)[0]
            assert proj.player.pro_team_abbrev == team

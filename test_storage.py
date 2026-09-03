"""
Unit tests for the SQLite storage layer, independent of any source
adapter — built by hand-constructing NormalizedObservation objects so
these tests don't depend on ESPN's data shape at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import storage
from normalized import (
    DataType,
    NormalizedObservation,
    PlayerIdentity,
    Position,
    RankingType,
)


def make_obs(**overrides) -> NormalizedObservation:
    defaults = dict(
        source="espn",
        season_id=2025,
        week_number=5,
        data_type=DataType.PROJECTION,
        player=PlayerIdentity(
            source_player_id="4429795",
            full_name="Jahmyr Gibbs",
            position=Position.RB,
            pro_team_id=8,
            pro_team_abbrev="DET",
        ),
        fantasy_points=22.06789034,
        scoring_format="PPR",
        retrieved_at=datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return NormalizedObservation(**defaults)


@pytest.fixture
def conn():
    c = storage.connect(":memory:")
    yield c
    c.close()


def test_insert_new_observation(conn):
    result = storage.save_observations(conn, [make_obs()])
    assert result == {"inserted": 1, "updated": 0, "total_rows_after": 1}


def test_reimporting_identical_data_does_not_duplicate(conn):
    storage.save_observations(conn, [make_obs()])
    result = storage.save_observations(conn, [make_obs()])
    assert result["inserted"] == 0
    assert result["updated"] == 1
    assert result["total_rows_after"] == 1  # not 2


def test_reimport_with_changed_value_updates_in_place(conn):
    storage.save_observations(conn, [make_obs(fantasy_points=22.0)])
    storage.save_observations(conn, [make_obs(fantasy_points=25.5)])
    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    assert len(rows) == 1
    assert rows[0].fantasy_points == pytest.approx(25.5)


def test_same_fact_different_sources_coexist(conn):
    espn_obs = make_obs(source="espn", fantasy_points=22.0)
    other_obs = make_obs(source="other_source", fantasy_points=19.5)
    result = storage.save_observations(conn, [espn_obs, other_obs])
    assert result == {"inserted": 2, "updated": 0, "total_rows_after": 2}

    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    by_source = {r.source: r.fantasy_points for r in rows}
    assert by_source == {"espn": pytest.approx(22.0), "other_source": pytest.approx(19.5)}


def test_projection_and_actual_are_distinct_rows_not_overwritten(conn):
    proj = make_obs(data_type=DataType.PROJECTION, fantasy_points=22.0)
    actual = make_obs(data_type=DataType.ACTUAL, fantasy_points=16.7)
    result = storage.save_observations(conn, [proj, actual])
    assert result["inserted"] == 2

    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    by_type = {r.data_type: r.fantasy_points for r in rows}
    assert by_type[DataType.PROJECTION] == pytest.approx(22.0)
    assert by_type[DataType.ACTUAL] == pytest.approx(16.7)


def test_ranking_type_none_vs_set_do_not_collide(conn):
    # This is the specific trap the module docstring warns about: two
    # RANKING rows with different ranking_type must not collide with
    # each other OR with a PROJECTION row (which has ranking_type=None).
    proj = make_obs(data_type=DataType.PROJECTION, ranking_type=None)
    overall = make_obs(data_type=DataType.RANKING, ranking_type=RankingType.OVERALL, rank=5.0, fantasy_points=None)
    positional = make_obs(data_type=DataType.RANKING, ranking_type=RankingType.RB, rank=3.0, fantasy_points=None)
    result = storage.save_observations(conn, [proj, overall, positional])
    assert result["inserted"] == 3

    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    assert len(rows) == 3


def test_same_player_week_different_scoring_formats_coexist(conn):
    # Backend v1 review (2026-09): scoring_format is now part of the
    # uniqueness key. Two observations that are identical in every
    # other respect but differ in scoring_format must NOT collide —
    # today every real adapter hardcodes "PPR", but the schema must not
    # silently overwrite a future Standard/Half-PPR import with a PPR one.
    ppr = make_obs(scoring_format="PPR", fantasy_points=22.0)
    standard = make_obs(scoring_format="Standard", fantasy_points=15.0)
    result = storage.save_observations(conn, [ppr, standard])
    assert result == {"inserted": 2, "updated": 0, "total_rows_after": 2}

    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    by_format = {r.scoring_format: r.fantasy_points for r in rows}
    assert by_format == {"PPR": pytest.approx(22.0), "Standard": pytest.approx(15.0)}


def test_reimporting_same_scoring_format_still_upserts(conn):
    # The flip side of the test above: this is still the SAME fact when
    # scoring_format matches, so re-importing it must still upsert, not
    # create a second row — the fix must not turn every re-import into
    # a duplicate.
    storage.save_observations(conn, [make_obs(scoring_format="PPR", fantasy_points=22.0)])
    result = storage.save_observations(conn, [make_obs(scoring_format="PPR", fantasy_points=23.5)])
    assert result["inserted"] == 0
    assert result["updated"] == 1
    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    assert len(rows) == 1
    assert rows[0].fantasy_points == pytest.approx(23.5)


def test_duplicate_within_one_batch_raises(conn):
    with pytest.raises(storage.DuplicateInBatchError):
        storage.save_observations(conn, [make_obs(), make_obs()])


def test_query_filters(conn):
    storage.save_observations(
        conn,
        [
            make_obs(week_number=5, data_type=DataType.PROJECTION),
            make_obs(week_number=6, data_type=DataType.PROJECTION),
            make_obs(week_number=5, data_type=DataType.ACTUAL),
        ],
    )
    assert len(storage.query_observations(conn, week_number=5)) == 2
    assert len(storage.query_observations(conn, week_number=5, data_type=DataType.ACTUAL)) == 1
    assert len(storage.query_observations(conn, week_number=99)) == 0


def test_roundtrip_preserves_raw_payload(conn):
    obs = make_obs(source_record_id="1120255", raw={"scoringPeriodId": 5, "appliedTotal": 22.06789034})
    storage.save_observations(conn, [obs])
    rows = storage.query_observations(conn, season_id=2025, week_number=5)
    assert rows[0].source_record_id == "1120255"
    assert rows[0].raw == {"scoringPeriodId": 5, "appliedTotal": 22.06789034}

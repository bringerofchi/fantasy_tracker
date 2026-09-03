"""
Unit tests for espn_core.py's stat-entry matching logic — specifically
the backend v1 review fix (2026-09) distinguishing "no matching weekly
stats[] entry" (expected absence: the period hasn't been played/
projected yet) from "more than one matching entry" (a data-integrity
anomaly). Before this fix both conditions raised the same
ESPNSchemaError, so a genuine anomaly in ACTUAL data would have been
silently swallowed by ESPNSourceAdapter.fetch()'s
`except ESPNSchemaError: pass` around ACTUAL parsing, indistinguishable
from an ordinary bye/unplayed week.

These tests use hand-built player-entry dicts rather than the real
fixture, so the anomaly case (which should never occur in real ESPN
data) can actually be constructed and exercised.
"""

from __future__ import annotations

import json

import pytest

from espn_core import (
    ESPNDataAnomalyError,
    ESPNSchemaError,
    STAT_SOURCE_ACTUAL,
    STAT_SOURCE_PROJECTED,
    STAT_SPLIT_WEEKLY,
    find_weekly_stat_entry,
)
from local_file_adapter import LocalFileSourceAdapter


def _entry_with_stats(stats: list[dict]) -> dict:
    return {
        "player": {
            "id": 1,
            "fullName": "Test Player",
            "defaultPositionId": 2,  # RB
            "stats": stats,
        }
    }


def _stat(season_id: int, week: int, stat_source_id: int, applied_total: float) -> dict:
    return {
        "seasonId": season_id,
        "scoringPeriodId": week,
        "statSourceId": stat_source_id,
        "statSplitTypeId": STAT_SPLIT_WEEKLY,
        "appliedTotal": applied_total,
        "id": f"{stat_source_id}test",
    }


class TestFindWeeklyStatEntry:
    def test_zero_matches_raises_schema_error_expected_absence(self):
        entry = _entry_with_stats([])
        with pytest.raises(ESPNSchemaError):
            find_weekly_stat_entry(entry, season_id=2025, week_number=5, stat_source_id=STAT_SOURCE_ACTUAL)

    def test_exactly_one_match_returns_it(self):
        stat = _stat(2025, 5, STAT_SOURCE_ACTUAL, 16.7)
        entry = _entry_with_stats([stat])
        result = find_weekly_stat_entry(entry, season_id=2025, week_number=5, stat_source_id=STAT_SOURCE_ACTUAL)
        assert result == stat

    def test_multiple_matches_raises_anomaly_not_schema_error(self):
        entry = _entry_with_stats([
            _stat(2025, 5, STAT_SOURCE_ACTUAL, 16.7),
            _stat(2025, 5, STAT_SOURCE_ACTUAL, 18.2),  # duplicate — should never happen
        ])
        with pytest.raises(ESPNDataAnomalyError):
            find_weekly_stat_entry(entry, season_id=2025, week_number=5, stat_source_id=STAT_SOURCE_ACTUAL)

    def test_anomaly_error_is_not_a_schema_error_subclass(self):
        # This is the whole point of the fix: code that does
        # `except ESPNSchemaError: pass` around an expected-absence case
        # must NOT also silently swallow a genuine data anomaly.
        assert not issubclass(ESPNDataAnomalyError, ESPNSchemaError)


class TestFetchPropagatesActualAnomaly:
    """
    End-to-end proof, not just a unit-level one: a duplicate ACTUAL
    entry for one player must abort the whole fetch() rather than being
    silently dropped the way an ordinary unplayed week is.
    """

    def test_fetch_raises_anomaly_instead_of_swallowing_it(self, tmp_path):
        fixture = {
            "players": [
                {
                    "player": {
                        "id": 1,
                        "fullName": "Anomaly Player",
                        "defaultPositionId": 2,
                        "stats": [
                            _stat(2025, 5, STAT_SOURCE_PROJECTED, 12.0),
                            _stat(2025, 5, STAT_SOURCE_ACTUAL, 16.7),
                            _stat(2025, 5, STAT_SOURCE_ACTUAL, 18.2),  # the anomaly
                        ],
                    }
                }
            ]
        }
        path = tmp_path / "anomaly_fixture.json"
        path.write_text(json.dumps(fixture))

        adapter = LocalFileSourceAdapter(path, include_rankings=False)
        with pytest.raises(ESPNDataAnomalyError):
            adapter.fetch(season_id=2025, week_number=5)

    def test_fetch_still_succeeds_and_swallows_a_genuinely_absent_actual(self, tmp_path):
        # Control case: zero ACTUAL entries (e.g. a future week) must
        # still behave exactly as before — projection comes through,
        # no exception propagates.
        fixture = {
            "players": [
                {
                    "player": {
                        "id": 1,
                        "fullName": "Not Yet Played",
                        "defaultPositionId": 2,
                        "stats": [
                            _stat(2025, 5, STAT_SOURCE_PROJECTED, 12.0),
                        ],
                    }
                }
            ]
        }
        path = tmp_path / "absent_actual_fixture.json"
        path.write_text(json.dumps(fixture))

        adapter = LocalFileSourceAdapter(path, include_rankings=False)
        observations = adapter.fetch(season_id=2025, week_number=5)
        assert len(observations) == 1  # projection only, no actual, no exception

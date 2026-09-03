"""
Integration tests for the ingestion pipeline (SourceAdapter -> storage),
using LocalFileSourceAdapter + the real ESPN fixture — no network needed.

These two tests are the ones that close out the QC items the ESPN
adapter's Phase 4C QC pass could only mark NOT TESTED, because at the
time there was no storage/ingestion layer to test against:

    QC5b: duplicate-on-INSERT is handled correctly at the DB/ingestion layer
    QC6b: no cross-contamination alongside OTHER source adapters

QC6b uses a small in-repo fake second adapter (_FakeOtherSourceAdapter)
rather than a real Yahoo/Athletic adapter, which don't exist yet. That's
intentional and disclosed: this proves the STORAGE LAYER doesn't
contaminate across sources — a genuine, real guarantee — not that a real
second source adapter has been built or validated. See README.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import storage
from ingestion import ingest
from local_file_adapter import LocalFileSourceAdapter
from normalized import (
    DataType,
    NormalizedObservation,
    PlayerIdentity,
    Position,
    RankingType,
    SourceAdapter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "qb_rb_wr_te_2025_2026_raw.json"


@pytest.fixture
def conn():
    c = storage.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def espn_adapter():
    # 2025 is a completed season (see FINDINGS.md) — no positional
    # rankings retained by ESPN for it, so include_rankings=False keeps
    # this fixture-backed adapter from raising on that documented gap.
    # Overall ranking (draftRanksByRankType) still comes through.
    return LocalFileSourceAdapter(FIXTURE, include_rankings=False)


class _FakeOtherSourceAdapter(SourceAdapter):
    """
    A minimal, deliberately fake second source — NOT a real Yahoo/Athletic
    integration. Exists only so cross-source attribution can be tested
    against something. Returns one projection for the same player/week
    ESPN covers, with an intentionally different value, so a passing test
    proves the two sources' numbers didn't get merged or overwritten.
    """
    source_name = "fake_other_source"

    def fetch(self, season_id: int, week_number: int) -> list[NormalizedObservation]:
        return [
            NormalizedObservation(
                source=self.source_name,
                season_id=season_id,
                week_number=week_number,
                data_type=DataType.PROJECTION,
                player=PlayerIdentity(
                    source_player_id="4429795",  # same ESPN id, reused as a stand-in cross-source key
                    full_name="Jahmyr Gibbs",
                    position=Position.RB,
                    pro_team_id=8,
                    pro_team_abbrev="DET",
                ),
                fantasy_points=19.9,  # deliberately different from ESPN's 22.06789034 for this player/week
                scoring_format="PPR",
                retrieved_at=datetime.now(timezone.utc),
            )
        ]


class TestDuplicateImportHandling:
    """QC5b: re-running ingestion for the same source/season/week must not duplicate rows."""

    def test_ingesting_same_week_twice_does_not_duplicate(self, conn, espn_adapter):
        result1 = ingest(espn_adapter, conn, season_id=2025, week_number=5)
        rows_after_first = storage.query_observations(conn, season_id=2025, week_number=5, source="espn")

        result2 = ingest(espn_adapter, conn, season_id=2025, week_number=5)
        rows_after_second = storage.query_observations(conn, season_id=2025, week_number=5, source="espn")

        assert result1.inserted > 0
        assert result1.updated == 0  # first run: everything is new
        assert result2.inserted == 0  # second run: nothing new
        assert result2.updated == result1.inserted  # everything that existed got re-affirmed, not duplicated
        assert len(rows_after_first) == len(rows_after_second)  # the actual DB-level guarantee

    def test_ingesting_three_times_is_stable(self, conn, espn_adapter):
        for _ in range(3):
            ingest(espn_adapter, conn, season_id=2025, week_number=5)
        rows = storage.query_observations(conn, season_id=2025, week_number=5, source="espn")
        row_count_1 = len(rows)
        ingest(espn_adapter, conn, season_id=2025, week_number=5)
        row_count_2 = len(storage.query_observations(conn, season_id=2025, week_number=5, source="espn"))
        assert row_count_1 == row_count_2


class TestCrossSourceAttribution:
    """QC6b: two sources reporting the same player/week must coexist, not contaminate."""

    def test_two_sources_same_player_week_both_persist_independently(self, conn, espn_adapter):
        ingest(espn_adapter, conn, season_id=2025, week_number=5)
        ingest(_FakeOtherSourceAdapter(), conn, season_id=2025, week_number=5)

        gibbs_rows = storage.query_observations(
            conn, season_id=2025, week_number=5, source_player_id="4429795"
        )
        gibbs_projections = [r for r in gibbs_rows if r.data_type == DataType.PROJECTION]
        by_source = {r.source: r.fantasy_points for r in gibbs_projections}

        assert by_source["espn"] == pytest.approx(22.06789034)
        assert by_source["fake_other_source"] == pytest.approx(19.9)
        # the critical assertion: neither value overwrote the other
        assert by_source["espn"] != by_source["fake_other_source"]

    def test_ingesting_other_source_does_not_touch_espn_row_count(self, conn, espn_adapter):
        ingest(espn_adapter, conn, season_id=2025, week_number=5)
        espn_count_before = len(storage.query_observations(conn, source="espn"))

        ingest(_FakeOtherSourceAdapter(), conn, season_id=2025, week_number=5)
        espn_count_after = len(storage.query_observations(conn, source="espn"))

        assert espn_count_before == espn_count_after


class TestIngestResult:
    def test_result_reports_accurate_counts(self, conn, espn_adapter):
        result = ingest(espn_adapter, conn, season_id=2025, week_number=5)
        assert result.source == "espn_fixture"
        assert result.fetched == result.inserted  # first run, nothing pre-existing
        assert result.by_data_type.get("projection", 0) == 4  # QB/RB/WR/TE, one each
        assert result.by_data_type.get("actual", 0) == 4

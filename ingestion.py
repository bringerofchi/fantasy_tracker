"""
Ingestion pipeline: SourceAdapter.fetch() -> storage.save_observations().

Deliberately thin. All the hard decisions (what counts as "the same
fact," how a re-import behaves, what ESPN's quirks mean) already live in
espn_core.py and storage.py — this module's only job is to call one then
the other and produce a result that's easy to log and assert against in
tests.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import storage
from normalized import DataType, SourceAdapter


@dataclass
class IngestResult:
    source: str
    season_id: int
    week_number: int
    fetched: int
    inserted: int
    updated: int
    by_data_type: dict[str, int]

    def __str__(self) -> str:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(self.by_data_type.items()))
        return (
            f"[{self.source}] season={self.season_id} week={self.week_number}: "
            f"fetched {self.fetched} ({breakdown}) -> {self.inserted} inserted, {self.updated} updated"
        )


def ingest(
    adapter: SourceAdapter,
    conn: sqlite3.Connection,
    season_id: int,
    week_number: int,
    **fetch_kwargs,
) -> IngestResult:
    """
    Run one adapter for one (season, week) and persist everything it
    returns. Does not catch exceptions from adapter.fetch() or from
    storage.save_observations() — both are designed to fail loudly
    (ESPNSchemaError, DuplicateInBatchError) rather than let bad data
    through silently, and that behavior should propagate here too.
    """
    observations = adapter.fetch(season_id, week_number, **fetch_kwargs)

    by_data_type: dict[str, int] = {}
    for o in observations:
        by_data_type[o.data_type.value] = by_data_type.get(o.data_type.value, 0) + 1

    result = storage.save_observations(conn, observations)

    return IngestResult(
        source=adapter.source_name,
        season_id=season_id,
        week_number=week_number,
        fetched=len(observations),
        inserted=result["inserted"],
        updated=result["updated"],
        by_data_type=by_data_type,
    )

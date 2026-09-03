"""
SQLite-backed storage for NormalizedObservation records.

Design choices, and why:

- One table, not one-per-source-or-data-type. The entire point of the
  normalized model is that storage doesn't need to know or care which
  source produced a fact — adding a second source adapter should never
  require a schema change here.

- A single UNIQUE constraint defines what "the same fact" means:
  (source, season_id, week_number, data_type, ranking_type, source_player_id).
  Re-ingesting the same fact UPSERTs (updates fantasy_points/rank/
  retrieved_at/raw) instead of inserting a duplicate row — this is what
  makes "duplicate imports handled correctly" (QC5b in the ESPN adapter's
  Phase 4C status) an actual, testable guarantee instead of a note that
  it couldn't be tested. See test_ingestion.py for the test that proves it.

- `source` is part of the uniqueness key specifically so that two
  different sources reporting the same player/week/data_type coexist as
  two separate rows rather than one clobbering the other — this is what
  makes cross-source attribution (QC6b) testable too.

- ranking_type is nullable in the data model (None for PROJECTION/ACTUAL)
  but SQLite treats every NULL as distinct for uniqueness purposes, which
  would silently break the intended uniqueness for exactly the rows where
  it's None. Stored as '' instead of NULL to avoid that trap.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from normalized import (
    DataType,
    NormalizedObservation,
    PlayerIdentity,
    Position,
    RankingType,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    season_id           INTEGER NOT NULL,
    week_number         INTEGER NOT NULL,
    data_type           TEXT NOT NULL,
    ranking_type        TEXT NOT NULL DEFAULT '',   -- '' stands in for "not a ranking"; see module docstring
    source_player_id    TEXT NOT NULL,
    player_full_name    TEXT NOT NULL,
    player_position     TEXT NOT NULL,
    player_pro_team_id  INTEGER,
    player_pro_team_abbrev TEXT,
    fantasy_points      REAL,
    scoring_format      TEXT,
    rank                REAL,
    retrieved_at        TEXT NOT NULL,
    source_record_id    TEXT,
    raw_json            TEXT,
    UNIQUE (source, season_id, week_number, data_type, ranking_type, source_player_id)
);

CREATE INDEX IF NOT EXISTS idx_observations_lookup
    ON observations (season_id, week_number, data_type, source_player_id);
"""

UPSERT_SQL = """
INSERT INTO observations (
    source, season_id, week_number, data_type, ranking_type, source_player_id,
    player_full_name, player_position, player_pro_team_id, player_pro_team_abbrev,
    fantasy_points, scoring_format, rank, retrieved_at, source_record_id, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source, season_id, week_number, data_type, ranking_type, source_player_id)
DO UPDATE SET
    player_full_name = excluded.player_full_name,
    player_position = excluded.player_position,
    player_pro_team_id = excluded.player_pro_team_id,
    player_pro_team_abbrev = excluded.player_pro_team_abbrev,
    fantasy_points = excluded.fantasy_points,
    scoring_format = excluded.scoring_format,
    rank = excluded.rank,
    retrieved_at = excluded.retrieved_at,
    source_record_id = excluded.source_record_id,
    raw_json = excluded.raw_json
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and ensure the schema exists."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def connect_ctx(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _row_key(o: NormalizedObservation) -> tuple:
    return (
        o.source,
        o.season_id,
        o.week_number,
        o.data_type.value,
        o.ranking_type.value if o.ranking_type else "",
        o.player.source_player_id,
    )


class DuplicateInBatchError(ValueError):
    """
    Raised when a single save_observations() call contains two
    observations with the same natural key. That's an ingestion bug (the
    adapter emitted the same fact twice in one fetch), not a normal
    re-import — a normal re-import is a SEPARATE call, which is exactly
    what the UPSERT logic is for. Catching it here, at write time, is
    cheaper than debugging silently-overwritten data later.
    """


def save_observations(conn: sqlite3.Connection, observations: list[NormalizedObservation]) -> dict:
    """
    Upsert every observation. Returns {"inserted": n, "updated": n,
    "total_rows_after": n} — "updated" means a row with that natural key
    already existed (a re-import or a value that changed), "inserted"
    means it's new.
    """
    seen_in_batch: dict[tuple, NormalizedObservation] = {}
    for o in observations:
        key = _row_key(o)
        if key in seen_in_batch:
            raise DuplicateInBatchError(
                f"observation batch contains the same natural key twice: {key} "
                f"({seen_in_batch[key].player.full_name} vs {o.player.full_name}) — "
                "this indicates the adapter emitted a duplicate fact in one fetch() call"
            )
        seen_in_batch[key] = o

    before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    inserted = 0
    updated = 0
    for o in observations:
        cur = conn.execute(
            "SELECT id FROM observations WHERE source=? AND season_id=? AND week_number=? "
            "AND data_type=? AND ranking_type=? AND source_player_id=?",
            _row_key(o),
        )
        existed = cur.fetchone() is not None
        conn.execute(
            UPSERT_SQL,
            (
                o.source,
                o.season_id,
                o.week_number,
                o.data_type.value,
                o.ranking_type.value if o.ranking_type else "",
                o.player.source_player_id,
                o.player.full_name,
                o.player.position.value,
                o.player.pro_team_id,
                o.player.pro_team_abbrev,
                o.fantasy_points,
                o.scoring_format,
                o.rank,
                o.retrieved_at.isoformat(),
                o.source_record_id,
                json.dumps(o.raw) if o.raw is not None else None,
            ),
        )
        if existed:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert after - before == inserted, (
        f"row count changed by {after - before}, expected {inserted} new rows — "
        "the UNIQUE constraint or upsert logic may not be doing what this function assumes"
    )
    return {"inserted": inserted, "updated": updated, "total_rows_after": after}


_POSITION_BY_VALUE = {p.value: p for p in Position}
_DATATYPE_BY_VALUE = {d.value: d for d in DataType}
_RANKINGTYPE_BY_VALUE = {r.value: r for r in RankingType}


def _row_to_observation(row: sqlite3.Row) -> NormalizedObservation:
    return NormalizedObservation(
        source=row["source"],
        season_id=row["season_id"],
        week_number=row["week_number"],
        data_type=_DATATYPE_BY_VALUE[row["data_type"]],
        player=PlayerIdentity(
            source_player_id=row["source_player_id"],
            full_name=row["player_full_name"],
            position=_POSITION_BY_VALUE[row["player_position"]],
            pro_team_id=row["player_pro_team_id"],
            pro_team_abbrev=row["player_pro_team_abbrev"],
        ),
        fantasy_points=row["fantasy_points"],
        scoring_format=row["scoring_format"],
        ranking_type=_RANKINGTYPE_BY_VALUE.get(row["ranking_type"]) if row["ranking_type"] else None,
        rank=row["rank"],
        retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
        source_record_id=row["source_record_id"],
        raw=json.loads(row["raw_json"]) if row["raw_json"] else None,
    )


def query_observations(
    conn: sqlite3.Connection,
    *,
    season_id: Optional[int] = None,
    week_number: Optional[int] = None,
    data_type: Optional[DataType] = None,
    source: Optional[str] = None,
    source_player_id: Optional[str] = None,
) -> list[NormalizedObservation]:
    """Simple filtered read. Every filter is optional and AND-combined."""
    conn.row_factory = sqlite3.Row
    clauses = []
    params: list = []
    if season_id is not None:
        clauses.append("season_id = ?")
        params.append(season_id)
    if week_number is not None:
        clauses.append("week_number = ?")
        params.append(week_number)
    if data_type is not None:
        clauses.append("data_type = ?")
        params.append(data_type.value)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if source_player_id is not None:
        clauses.append("source_player_id = ?")
        params.append(source_player_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM observations {where}", params).fetchall()
    return [_row_to_observation(r) for r in rows]

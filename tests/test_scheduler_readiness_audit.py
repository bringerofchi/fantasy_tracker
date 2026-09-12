"""
Phase 4A -> 4B scheduler-readiness audit tests. These verify the specific
areas requested in the pre-4B review: transaction boundaries, concurrent
execution (the pending-review duplicate race), collection_runs state
transitions on unexpected failure, and provenance sufficiency.
"""
import unittest
import tempfile
import os
import json
import sqlite3
from db.database import init_db, get_conn
from db import repository as repo
from ingestion.collector import LocalFileSourceAdapter
from ingestion import pipeline


def write_source_file(path, entries):
    with open(path, "w") as f:
        json.dump(entries, f)


class TestTransactionBoundariesAndCrashRecovery(unittest.TestCase):
    """A Python-level exception mid-batch (bug, DB error) must not leave
    collection_runs stuck at 'started' forever — a scheduler needs an
    accurate terminal status to do run-locking / safe restart correctly."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.src_path = tempfile.mktemp(suffix=".json")

    def tearDown(self):
        self.conn.close()
        for p in (self.db_path, self.src_path):
            if os.path.exists(p):
                os.remove(p)

    def test_unexpected_exception_mid_batch_marks_run_error_not_stuck_started(self):
        repo.create_player(self.conn, "Crash WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Crash WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
            {"player_name": "Crash WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receptions", "value": 6, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)

        orig_resolve = repo.resolve_player
        calls = {"n": 0}
        def flaky_resolve(conn, name, position):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash mid-batch")
            return orig_resolve(conn, name, position)
        repo.resolve_player = flaky_resolve
        try:
            result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        finally:
            repo.resolve_player = orig_resolve

        # Returns a graceful result (Phase 4B refinement) rather than raising —
        # an orchestrator needs every call to come back as an actionable result.
        self.assertTrue(result.get("processing_error"))
        run = self.conn.execute("SELECT status, completed_at FROM collection_runs").fetchone()
        self.assertEqual(run["status"], "error")
        self.assertIsNotNone(run["completed_at"])  # reached a terminal state, not stuck at 'started'

    def test_exception_does_not_corrupt_already_committed_observations(self):
        """The one observation that passed all checks before the crash stays
        correctly trusted — an in-process crash doesn't roll back or corrupt
        individually-valid, already-committed data."""
        repo.create_player(self.conn, "Crash RB", "RB")
        write_source_file(self.src_path, [
            {"player_name": "Crash RB", "position": "RB", "week_number": 1, "data_type": "actual",
             "stat_name": "rush_yards", "value": 90, "confidence": 0.99},
            {"player_name": "Crash RB", "position": "RB", "week_number": 1, "data_type": "actual",
             "stat_name": "rush_tds", "value": 1, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)

        orig_resolve = repo.resolve_player
        calls = {"n": 0}
        def flaky_resolve(conn, name, position):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash")
            return orig_resolve(conn, name, position)
        repo.resolve_player = flaky_resolve
        try:
            pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        finally:
            repo.resolve_player = orig_resolve

        row = self.conn.execute("SELECT stat_value FROM statistics WHERE stat_name='rush_yards'").fetchone()
        self.assertEqual(row["stat_value"], 90)


class TestFindStaleCollectionRuns(unittest.TestCase):
    """Read-only visibility helper a Phase 4B scheduler needs for 'safe restart
    after interruption' — detecting runs a hard process kill left stuck at
    'started' (which no amount of in-process exception handling can catch)."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_recent_started_run_is_not_flagged_stale(self):
        source_id = self.conn.execute("SELECT source_id FROM sources WHERE name='NFL'").fetchone()["source_id"]
        self.conn.execute("INSERT INTO collection_runs (source_id, season_id, status) VALUES (?, ?, 'started')",
                           (source_id, self.season_id))
        self.conn.commit()
        stale = pipeline.find_stale_collection_runs(self.conn, stale_minutes=60)
        self.assertEqual(stale, [])

    def test_old_started_run_is_flagged_stale(self):
        source_id = self.conn.execute("SELECT source_id FROM sources WHERE name='NFL'").fetchone()["source_id"]
        self.conn.execute(
            "INSERT INTO collection_runs (source_id, season_id, status, started_at) "
            "VALUES (?, ?, 'started', datetime('now', '-3 hours'))",
            (source_id, self.season_id),
        )
        self.conn.commit()
        stale = pipeline.find_stale_collection_runs(self.conn, stale_minutes=60)
        self.assertEqual(len(stale), 1)

    def test_completed_run_never_flagged_stale_regardless_of_age(self):
        source_id = self.conn.execute("SELECT source_id FROM sources WHERE name='NFL'").fetchone()["source_id"]
        self.conn.execute(
            "INSERT INTO collection_runs (source_id, season_id, status, started_at, completed_at) "
            "VALUES (?, ?, 'successful', datetime('now', '-3 hours'), datetime('now', '-3 hours'))",
            (source_id, self.season_id),
        )
        self.conn.commit()
        stale = pipeline.find_stale_collection_runs(self.conn, stale_minutes=60)
        self.assertEqual(stale, [])


class TestConcurrentDuplicateReviewGuard(unittest.TestCase):
    """The app-level 'does a pending duplicate already exist' check has a
    TOCTOU race under real concurrency. A DB-level partial unique index is
    the actual guarantee; this proves it holds even when the app-level
    check is bypassed entirely (simulating the race having already occurred)."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        self.import_id = self.conn.execute(
            "INSERT INTO imports (file_name, file_path, file_hash, file_type, status) VALUES ('x','x','x','x','uploaded')"
        ).lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _insert_pending(self, value):
        cur = self.conn.execute(
            """INSERT INTO proposed_observations
               (import_id, extracted_player_name, identity_match_type, position, data_type, season_id, week_id,
                source_name, stat_name, stat_value, field_confidence, validation_flags_json, disposition)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.import_id, "Racey Guy", "none", "WR", "actual", self.season_id, self.week1,
             "NFL", "receiving_yards", value, 0.5, "[]", "pending"),
        )
        return cur.lastrowid

    def test_db_level_unique_index_blocks_second_identical_pending_transition(self):
        pid_a = self._insert_pending(60)
        pid_b = self._insert_pending(60)  # same player/week/stat/value — simulates two racing writers
        self.conn.commit()

        self.conn.execute("UPDATE proposed_observations SET disposition='sent_to_review' WHERE proposed_id=?", (pid_a,))
        self.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE proposed_observations SET disposition='sent_to_review' WHERE proposed_id=?", (pid_b,))

    def test_route_field_race_is_handled_gracefully_not_a_crash(self):
        """Exercises the actual code path: _route_field must catch the
        IntegrityError itself and resolve to a safe outcome rather than
        propagating a raw sqlite3 exception out of the pipeline."""
        pid_a = self._insert_pending(60)
        self.conn.execute("UPDATE proposed_observations SET disposition='sent_to_review' WHERE proposed_id=?", (pid_a,))
        self.conn.execute(
            "INSERT INTO review_items (entity_type, entity_id, reason, confidence, details_json, status) "
            "VALUES ('proposed_observation', ?, 'player_not_found', 'Medium', '{}', 'pending')", (pid_a,),
        )
        self.conn.commit()

        # A second field with identical identity/value goes through the real
        # routing function — must not raise, and must not leave two pending items.
        disposition, proposed_id = pipeline._route_field(
            self.conn, self.import_id, "Racey Guy", None, "none", "WR", "actual",
            self.season_id, self.week1, 1, None, "NFL", "receiving_yards", 60, 0.5, [], 0.85,
        )
        self.assertEqual(disposition, "duplicate_race")
        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM review_items WHERE status='pending' AND entity_type='proposed_observation'"
        ).fetchone()["c"]
        self.assertEqual(pending, 1)  # still just one, not two

    def test_ingest_collector_batch_end_to_end_never_produces_duplicate_pending_items(self):
        """Belt-and-suspenders: running the full pipeline function many times
        against identical unresolved data never accumulates more than one
        pending review item for it, regardless of the app-level check's races."""
        src_path = tempfile.mktemp(suffix=".json")
        write_source_file(src_path, [
            {"player_name": "Repeatedly Unresolved", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 60, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", src_path)
        try:
            for _ in range(5):
                pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
            pending = self.conn.execute(
                "SELECT COUNT(*) c FROM review_items WHERE status='pending' AND entity_type='proposed_observation'"
            ).fetchone()["c"]
            self.assertEqual(pending, 1)
        finally:
            os.remove(src_path)


class TestProjectionProvenance(unittest.TestCase):
    """Audit finding: statistics had a structured provenance_ref; projections
    only had a free-text notes field. Confirms parity now exists."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_projection_stores_structured_provenance_ref(self):
        pid = repo.create_player(self.conn, "Provenance Test WR", "WR")
        repo.add_projection(self.conn, pid, self.season_id, self.week1, "receiving_yards", 70,
                             source_type="ai_extracted", source_name="Yahoo", provenance_ref="import:42")
        row = self.conn.execute("SELECT provenance_ref FROM projections WHERE player_id=?", (pid,)).fetchone()
        self.assertEqual(row["provenance_ref"], "import:42")

    def test_collector_batch_projection_has_provenance_ref(self):
        repo.create_player(self.conn, "Collector Proj WR", "WR")
        src_path = tempfile.mktemp(suffix=".json")
        write_source_file(src_path, [
            {"player_name": "Collector Proj WR", "position": "WR", "week_number": 1, "data_type": "projection",
             "stat_name": "receiving_yards", "value": 70, "confidence": 0.95},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", src_path)
        try:
            result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
            self.assertEqual(len(result["auto_accepted"]), 1)
            row = self.conn.execute("SELECT provenance_ref FROM projections WHERE source_name='Yahoo'").fetchone()
            self.assertEqual(row["provenance_ref"], f"import:{result['import_id']}")
        finally:
            os.remove(src_path)


if __name__ == "__main__":
    unittest.main()

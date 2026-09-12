"""
Phase 4C season-scope extension: season-long/preseason projections and
rankings, alongside the existing weekly ones. Proves the same lifecycle
rigor as the ranking_type extension: validation, versioning/idempotency
(with the NULL-safety fix that scope='season' rows need), auto-accept,
review, correction, rejection, provenance — all through the same
_route_field core, no parallel pipeline for season-scope data.
"""
import unittest
import tempfile
import os
import json
import sqlite3
from db.database import init_db, get_conn
from db import repository as repo
from ingestion.collector import LocalFileSourceAdapter, NormalizedObservation
from ingestion import pipeline


def write_source_file(path, entries):
    with open(path, "w") as f:
        json.dump(entries, f)


class SeasonScopeTestBase(unittest.TestCase):
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


class TestScopeValidation(unittest.TestCase):
    def test_weekly_requires_week_id(self):
        with self.assertRaises(ValueError):
            repo.validate_scope_week_consistency("weekly", None)

    def test_season_forbids_week_id(self):
        with self.assertRaises(ValueError):
            repo.validate_scope_week_consistency("season", 7)

    def test_weekly_with_week_id_passes(self):
        repo.validate_scope_week_consistency("weekly", 7)  # no raise

    def test_season_with_no_week_id_passes(self):
        repo.validate_scope_week_consistency("season", None)  # no raise

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            repo.validate_scope_week_consistency("monthly", None)


class TestSeasonScopeRepositoryLayer(SeasonScopeTestBase):
    def test_season_projection_insert_and_retrieve(self):
        pid = repo.create_player(self.conn, "Season QB", "QB")
        new_id, _ = repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4200,
                                      source_type="imported", source_name="The Athletic", scope="season")
        row = self.conn.execute("SELECT * FROM projections WHERE projection_id=?", (new_id,)).fetchone()
        self.assertEqual(row["scope"], "season")
        self.assertIsNone(row["week_id"])
        self.assertEqual(row["projected_value"], 4200)

    def test_season_projection_rejects_a_week_id(self):
        pid = repo.create_player(self.conn, "Bad Season QB", "QB")
        week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        with self.assertRaises(ValueError):
            repo.add_projection(self.conn, pid, self.season_id, week1, "pass_yards", 4200, scope="season")

    def test_weekly_projection_still_requires_week_id(self):
        pid = repo.create_player(self.conn, "Weekly WR", "WR")
        with self.assertRaises(ValueError):
            repo.add_projection(self.conn, pid, self.season_id, None, "receiving_yards", 80, scope="weekly")

    def test_season_ranking_insert_and_retrieve(self):
        pid = repo.create_player(self.conn, "Season Ranked RB", "RB")
        new_id, _ = repo.add_ranking(self.conn, pid, self.season_id, None, "The Athletic", 3, "RB",
                                      scope="season", verification_status="verified")
        row = self.conn.execute("SELECT * FROM rankings WHERE ranking_id=?", (new_id,)).fetchone()
        self.assertEqual(row["scope"], "season")
        self.assertIsNone(row["week_id"])
        self.assertEqual(row["ranking_type"], "RB")

    def test_weekly_and_season_projections_for_same_stat_coexist_independently(self):
        """A player can have BOTH a preseason season-long projection AND a
        current week's projection at the same time — these must not collide
        or be treated as the same observation."""
        pid = repo.create_player(self.conn, "Dual Scope WR", "WR")
        week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        repo.add_projection(self.conn, pid, self.season_id, None, "receiving_yards", 1200, scope="season")
        repo.add_projection(self.conn, pid, self.season_id, week1, "receiving_yards", 85, scope="weekly")
        rows = self.conn.execute(
            "SELECT scope, projected_value FROM projections WHERE player_id=? AND is_current=1 ORDER BY scope", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        by_scope = {r["scope"]: r["projected_value"] for r in rows}
        self.assertEqual(by_scope["season"], 1200)
        self.assertEqual(by_scope["weekly"], 85)


class TestSeasonScopeNullSafetyVersioning(SeasonScopeTestBase):
    """The exact class of bug already fixed once for ranking_type: a bare
    'week_id=?' comparison is never true against NULL, so without the fix
    (week_id IS ?), season-scope rows would never be recognized as existing,
    breaking both idempotency and conflict detection for every one of them."""

    def test_identical_season_projection_reentry_is_now_idempotent(self):
        """Historical note: add_projection previously had no idempotent no-op
        on an identical value, unlike add_statistic/add_ranking — every
        re-entry versioned regardless of whether the value actually changed.
        This became a real, concrete problem once re-imports of unchanged
        real source data started actually happening (re-running an identical
        real ESPN page created 116 meaningless duplicate versions). Fixed:
        add_projection now matches add_statistic/add_ranking's idempotency
        convention. The IS-based NULL-safe lookup is still what makes this
        correctly find the EXISTING row to compare against in the first
        place — without that fix, this identity would never be found for a
        season-scope row and every re-entry would look like a fresh insert."""
        pid = repo.create_player(self.conn, "Idempotent Season QB", "QB")
        id1, was_new1 = repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4200, scope="season")
        id2, was_new2 = repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4200, scope="season")
        self.assertTrue(was_new1)
        self.assertFalse(was_new2)  # identical re-entry — no-op, not a new version
        self.assertEqual(id1, id2)
        rows = self.conn.execute(
            "SELECT is_current, observation_version FROM projections WHERE player_id=? ORDER BY projection_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 1)  # no spurious second version
        self.assertEqual(rows[0]["is_current"], 1)
        self.assertEqual(rows[0]["observation_version"], 1)

    def test_differing_season_projection_reentry_still_versions_correctly(self):
        """A genuinely DIFFERENT value must still version, exactly as before —
        the idempotency fix must not accidentally suppress real corrections."""
        pid = repo.create_player(self.conn, "Changing Season QB Idempotency", "QB")
        repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4200, scope="season")
        id2, was_new2 = repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4350, scope="season")
        self.assertTrue(was_new2)
        rows = self.conn.execute(
            "SELECT is_current, observation_version FROM projections WHERE player_id=? ORDER BY projection_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["is_current"], 1)
        self.assertEqual(rows[1]["observation_version"], 2)  # correctly recognized as a NEW VERSION of the same row, not an unrelated insert

    def test_differing_season_projection_reentry_versions_correctly(self):
        pid = repo.create_player(self.conn, "Changing Season QB", "QB")
        repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4200, scope="season")
        repo.add_projection(self.conn, pid, self.season_id, None, "pass_yards", 4350, scope="season")
        rows = self.conn.execute(
            "SELECT projected_value, is_current FROM projections WHERE player_id=? ORDER BY projection_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["projected_value"], 4200)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["projected_value"], 4350)
        self.assertEqual(rows[1]["is_current"], 1)

    def test_identical_season_ranking_reentry_is_idempotent(self):
        pid = repo.create_player(self.conn, "Idempotent Season RB", "RB")
        repo.add_ranking(self.conn, pid, self.season_id, None, "The Athletic", 5, "RB", scope="season")
        repo.add_ranking(self.conn, pid, self.season_id, None, "The Athletic", 5, "RB", scope="season")
        rows = self.conn.execute("SELECT COUNT(*) c FROM rankings WHERE player_id=?", (pid,)).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_differing_season_ranking_reentry_flags_conflict_not_silent_overwrite(self):
        pid = repo.create_player(self.conn, "Changing Season RB", "RB")
        repo.add_ranking(self.conn, pid, self.season_id, None, "The Athletic", 5, "RB", scope="season")
        repo.add_ranking(self.conn, pid, self.season_id, None, "The Athletic", 8, "RB", scope="season")
        rows = self.conn.execute(
            "SELECT ranking_value, is_current FROM rankings WHERE player_id=? ORDER BY ranking_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["is_current"], 1)
        conflict = self.conn.execute(
            "SELECT * FROM review_items WHERE entity_type='ranking' AND reason='conflict' AND status='pending'"
        ).fetchone()
        self.assertIsNotNone(conflict)

    def test_pending_duplicate_unique_index_catches_season_scope_rows(self):
        """Directly proves the COALESCE(week_id, -1) fix on idx_unique_pending_proposal:
        without it, two season-scope pending proposals for the same observation
        would never collide, since SQLite never treats two NULLs as equal in a
        plain unique index."""
        import_id = self.conn.execute(
            "INSERT INTO imports (file_name, file_path, file_hash, file_type, status) VALUES ('x','x','x','x','uploaded')"
        ).lastrowid
        self.conn.commit()

        def insert_pending(value):
            cur = self.conn.execute(
                """INSERT INTO proposed_observations
                   (import_id, extracted_player_name, identity_match_type, position, data_type, scope,
                    season_id, week_id, source_name, stat_name, stat_value, field_confidence,
                    validation_flags_json, disposition)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (import_id, "Season Guy", "none", "QB", "projection", "season",
                 self.season_id, None, "The Athletic", "pass_yards", value, 0.5, "[]", "pending"),
            )
            return cur.lastrowid

        pid_a = insert_pending(4200)
        pid_b = insert_pending(4200)
        self.conn.commit()
        self.conn.execute("UPDATE proposed_observations SET disposition='sent_to_review' WHERE proposed_id=?", (pid_a,))
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE proposed_observations SET disposition='sent_to_review' WHERE proposed_id=?", (pid_b,))


class TestSeasonScopeIngestionPipeline(SeasonScopeTestBase):
    def test_season_projection_auto_accepts_through_local_file_adapter(self):
        repo.create_player(self.conn, "Auto Season QB", "QB")
        write_source_file(self.src_path, [
            {"player_name": "Auto Season QB", "position": "QB", "data_type": "projection",
             "stat_name": "pass_yards", "value": 4200, "confidence": 0.95, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(result["auto_accepted"]), 1)
        row = self.conn.execute("SELECT * FROM projections WHERE scope='season' AND is_current=1").fetchone()
        self.assertEqual(row["projected_value"], 4200)
        self.assertIsNone(row["week_id"])

    def test_season_ranking_auto_accepts_through_local_file_adapter(self):
        repo.create_player(self.conn, "Auto Season RB", "RB")
        write_source_file(self.src_path, [
            {"player_name": "Auto Season RB", "position": "RB", "data_type": "ranking",
             "ranking_type": "RB", "value": 3, "confidence": 0.95, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(result["auto_accepted"]), 1)
        row = self.conn.execute("SELECT * FROM rankings WHERE scope='season' AND is_current=1").fetchone()
        self.assertEqual(row["ranking_value"], 3)

    def test_actual_stat_with_season_scope_is_malformed_not_silently_accepted(self):
        write_source_file(self.src_path, [
            {"player_name": "Nobody", "position": "WR", "data_type": "actual",
             "stat_name": "receiving_yards", "value": 900, "confidence": 0.95, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(result["auto_accepted"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("scope='season'", result["errors"][0]["reason"])

    def test_low_confidence_season_projection_goes_to_review(self):
        repo.create_player(self.conn, "Uncertain Season WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Uncertain Season WR", "position": "WR", "data_type": "projection",
             "stat_name": "receiving_yards", "value": 1100, "confidence": 0.4, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(result["auto_accepted"], [])
        self.assertEqual(len(result["sent_to_review"]), 1)
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        details = json.loads(item["details_json"])
        self.assertEqual(details["scope"], "season")
        self.assertIsNone(details["week_number"])

    def test_accept_season_projection_with_correction(self):
        repo.create_player(self.conn, "Correction Season QB", "QB")
        write_source_file(self.src_path, [
            {"player_name": "Correction Season QB", "position": "QB", "data_type": "projection",
             "stat_name": "pass_yards", "value": 4200, "confidence": 0.4, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed_id = result["sent_to_review"][0]
        new_id = pipeline.accept_proposed_observation(self.conn, proposed_id, corrected_value=4350)
        row = self.conn.execute("SELECT projected_value, scope FROM projections WHERE projection_id=?", (new_id,)).fetchone()
        self.assertEqual(row["projected_value"], 4350)
        self.assertEqual(row["scope"], "season")

    def test_reject_season_projection_creates_no_trusted_row(self):
        repo.create_player(self.conn, "Rejected Season QB", "QB")
        write_source_file(self.src_path, [
            {"player_name": "Rejected Season QB", "position": "QB", "data_type": "projection",
             "stat_name": "pass_yards", "value": 4200, "confidence": 0.4, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed_id = result["sent_to_review"][0]
        pipeline.reject_proposed_observation(self.conn, proposed_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM projections").fetchone()["c"], 0)

    def test_season_projection_full_provenance_chain(self):
        repo.create_player(self.conn, "Provenance Season QB", "QB")
        write_source_file(self.src_path, [
            {"player_name": "Provenance Season QB", "position": "QB", "data_type": "projection",
             "stat_name": "pass_yards", "value": 4200, "confidence": 0.95, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed = self.conn.execute("SELECT * FROM proposed_observations WHERE import_id=?", (result["import_id"],)).fetchone()
        self.assertEqual(proposed["scope"], "season")
        trusted = self.conn.execute("SELECT * FROM projections WHERE projection_id=?", (proposed["resulting_record_id"],)).fetchone()
        self.assertEqual(trusted["provenance_ref"], f"import:{result['import_id']}")

    def test_mixed_weekly_and_season_batch_processed_correctly_together(self):
        repo.create_player(self.conn, "Mixed WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Mixed WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.95},
            {"player_name": "Mixed WR", "position": "WR", "data_type": "projection",
             "stat_name": "receiving_yards", "value": 1150, "confidence": 0.95, "scope": "season"},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(result["auto_accepted"]), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM projections WHERE scope='season'").fetchone()["c"], 1)


if __name__ == "__main__":
    unittest.main()

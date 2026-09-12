"""
Phase 4C prerequisite: Ranking Ingestion Extension.

Proves the full proposed-observation lifecycle for data_type='ranking':
creation, persistence, validation, auto-accept, manual accept,
accept-with-correction, identity resolution, rejection, conflict handling,
provenance, and versioning/idempotency — mirroring exactly what's already
proven for 'actual'/'projection' observations, through the same _route_field
core, not a parallel ranking-specific pipeline.
"""
import unittest
import tempfile
import os
import json
from db.database import init_db, get_conn
from db import repository as repo
from ingestion.collector import LocalFileSourceAdapter
from ingestion import pipeline


def write_source_file(path, entries):
    with open(path, "w") as f:
        json.dump(entries, f)


class RankingTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        self.src_path = tempfile.mktemp(suffix=".json")

    def tearDown(self):
        self.conn.close()
        for p in (self.db_path, self.src_path):
            if os.path.exists(p):
                os.remove(p)


class TestValidateRankingValue(unittest.TestCase):
    def test_none_passes(self):
        repo.validate_ranking_value(None)  # no raise

    def test_positive_integer_passes(self):
        repo.validate_ranking_value(1)
        repo.validate_ranking_value(150)

    def test_zero_rejected(self):
        with self.assertRaises(ValueError):
            repo.validate_ranking_value(0)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            repo.validate_ranking_value(-3)

    def test_fractional_rejected(self):
        with self.assertRaises(ValueError):
            repo.validate_ranking_value(5.5)

    def test_whole_number_as_float_passes(self):
        repo.validate_ranking_value(7.0)  # no raise — same integer value, just a float type


class TestRankingAutoAccept(RankingTestBase):
    def test_high_confidence_ranking_auto_accepts(self):
        repo.create_player(self.conn, "Ranked WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Ranked WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "overall", "value": 5, "confidence": 0.97},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(result["auto_accepted"]), 1)

        row = self.conn.execute("SELECT * FROM rankings WHERE is_current=1").fetchone()
        self.assertEqual(row["ranking_value"], 5)
        self.assertEqual(row["ranking_type"], "overall")

    def test_ranking_never_leaks_into_statistics_or_projections(self):
        repo.create_player(self.conn, "Ranked RB", "RB")
        write_source_file(self.src_path, [
            {"player_name": "Ranked RB", "position": "RB", "week_number": 1, "data_type": "ranking",
             "ranking_type": "RB", "value": 3, "confidence": 0.97},
        ])
        adapter = LocalFileSourceAdapter("ESPN", self.src_path)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM projections").fetchone()["c"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM rankings").fetchone()["c"], 1)


class TestRankingReview(RankingTestBase):
    def test_low_confidence_ranking_goes_to_review_with_ranking_type_visible(self):
        repo.create_player(self.conn, "Uncertain WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Uncertain WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "WR", "value": 12, "confidence": 0.4},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(result["auto_accepted"], [])
        self.assertEqual(len(result["sent_to_review"]), 1)

        item = self.conn.execute(
            "SELECT * FROM review_items WHERE entity_type='proposed_observation'"
        ).fetchone()
        details = json.loads(item["details_json"])
        self.assertEqual(details["ranking_type"], "WR")
        self.assertEqual(details["stat_value"], 12)

    def test_unresolved_player_ranking_goes_to_review_not_auto_created(self):
        write_source_file(self.src_path, [
            {"player_name": "Nobody Ranked", "position": "TE", "week_number": 1, "data_type": "ranking",
             "ranking_type": "TE", "value": 4, "confidence": 0.97},
        ])
        adapter = LocalFileSourceAdapter("ESPN", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(result["auto_accepted"], [])
        exists = self.conn.execute("SELECT * FROM players WHERE display_name='Nobody Ranked'").fetchone()
        self.assertIsNone(exists)


class TestRankingCorrectionAndRejection(RankingTestBase):
    def test_accept_ranking_with_correction(self):
        repo.create_player(self.conn, "Correction WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Correction WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "overall", "value": 20, "confidence": 0.5},  # low confidence -> review
        ])
        adapter = LocalFileSourceAdapter("The Athletic", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed_id = result["sent_to_review"][0]

        # reviewer corrects the misread rank 20 -> 12
        new_id = pipeline.accept_proposed_observation(self.conn, proposed_id, corrected_value=12)
        trusted = self.conn.execute("SELECT ranking_value, verification_status, provenance_ref FROM rankings WHERE ranking_id=?",
                                     (new_id,)).fetchone()
        self.assertEqual(trusted["ranking_value"], 12)
        self.assertEqual(trusted["verification_status"], "verified")
        self.assertIn("reviewed", trusted["provenance_ref"])

    def test_reject_ranking_creates_no_trusted_row(self):
        repo.create_player(self.conn, "Rejected WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Rejected WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "overall", "value": 20, "confidence": 0.5},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed_id = result["sent_to_review"][0]

        pipeline.reject_proposed_observation(self.conn, proposed_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM rankings").fetchone()["c"], 0)
        prop = self.conn.execute("SELECT disposition FROM proposed_observations WHERE proposed_id=?", (proposed_id,)).fetchone()
        self.assertEqual(prop["disposition"], "rejected")

    def test_invalid_corrected_ranking_value_rejected_at_accept_time(self):
        repo.create_player(self.conn, "BadCorrection WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "BadCorrection WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "overall", "value": 20, "confidence": 0.5},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed_id = result["sent_to_review"][0]
        with self.assertRaises(ValueError):
            pipeline.accept_proposed_observation(self.conn, proposed_id, corrected_value=-5)


class TestRankingVersioningAndConflict(RankingTestBase):
    def test_identical_reentry_is_idempotent(self):
        pid = repo.create_player(self.conn, "Stable WR", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        rows = self.conn.execute("SELECT * FROM rankings WHERE player_id=?", (pid,)).fetchall()
        self.assertEqual(len(rows), 1)
        pending = self.conn.execute("SELECT COUNT(*) c FROM review_items WHERE entity_type='ranking'").fetchone()["c"]
        self.assertEqual(pending, 0)

    def test_differing_reentry_versions_and_flags_conflict_not_silent_overwrite(self):
        pid = repo.create_player(self.conn, "Changing WR", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 8, "overall", verification_status="verified")

        rows = self.conn.execute(
            "SELECT ranking_value, is_current FROM rankings WHERE player_id=? ORDER BY ranking_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ranking_value"], 5)
        self.assertEqual(rows[0]["is_current"], 0)   # preserved, not deleted
        self.assertEqual(rows[1]["ranking_value"], 8)
        self.assertEqual(rows[1]["is_current"], 1)   # new value trusted immediately (pending possible reversal)

        conflict = self.conn.execute(
            "SELECT * FROM review_items WHERE entity_type='ranking' AND reason='conflict' AND status='pending'"
        ).fetchone()
        self.assertIsNotNone(conflict)  # flagged, exactly like statistics — NOT silently overwritten

    def test_different_ranking_types_same_value_do_not_collide(self):
        """A WR-position rank of 5 and an overall rank of 5 for the same
        player/week are different facts and must not be treated as the same
        observation by either the idempotency check or the conflict path."""
        pid = repo.create_player(self.conn, "MultiRank WR", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "WR", verification_status="verified")
        rows = self.conn.execute("SELECT ranking_type, ranking_value FROM rankings WHERE player_id=?", (pid,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["ranking_type"] for r in rows}, {"overall", "WR"})
        # no spurious conflict between the two distinct ranking types
        conflicts = self.conn.execute("SELECT COUNT(*) c FROM review_items WHERE entity_type='ranking'").fetchone()["c"]
        self.assertEqual(conflicts, 0)

    def test_different_ranking_types_same_value_dont_falsely_dedupe_pending_review(self):
        """Same guarantee as above, but through the collector batch's pending-review
        path (both the app-level fast-check and the DB-level unique index)."""
        write_source_file(self.src_path, [
            {"player_name": "Ambiguous Ranked WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "overall", "value": 5, "confidence": 0.4},
            {"player_name": "Ambiguous Ranked WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "WR", "value": 5, "confidence": 0.4},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(result["sent_to_review"]), 2)  # NOT deduped against each other
        self.assertEqual(result["duplicates_skipped"], [])


class TestRankingConflictResolution(RankingTestBase):
    def test_rejecting_a_ranking_conflict_reverts_trust(self):
        pid = repo.create_player(self.conn, "Revert WR", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 8, "overall", verification_status="verified")
        review_id = self.conn.execute(
            "SELECT review_id FROM review_items WHERE entity_type='ranking' AND status='pending'"
        ).fetchone()["review_id"]

        repo.resolve_ranking_conflict(self.conn, review_id, "rejected")
        trusted = self.conn.execute(
            "SELECT ranking_value FROM rankings WHERE player_id=? AND is_current=1", (pid,)
        ).fetchone()
        self.assertEqual(trusted["ranking_value"], 5)  # reverted to the original
        rows = self.conn.execute("SELECT COUNT(*) c FROM rankings WHERE player_id=?", (pid,)).fetchone()["c"]
        self.assertEqual(rows, 2)  # both preserved, nothing deleted

    def test_approving_a_ranking_conflict_keeps_new_value_trusted(self):
        pid = repo.create_player(self.conn, "Approve WR", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 8, "overall", verification_status="verified")
        review_id = self.conn.execute(
            "SELECT review_id FROM review_items WHERE entity_type='ranking' AND status='pending'"
        ).fetchone()["review_id"]

        repo.resolve_ranking_conflict(self.conn, review_id, "approved")
        trusted = self.conn.execute(
            "SELECT ranking_value, verification_status FROM rankings WHERE player_id=? AND is_current=1", (pid,)
        ).fetchone()
        self.assertEqual(trusted["ranking_value"], 8)
        self.assertEqual(trusted["verification_status"], "verified")

    def test_resolve_ranking_conflict_via_http_review_route(self):
        """End-to-end through the actual Flask route, not just the repository layer."""
        import app as flask_app
        import db.database as dbmod
        pid = repo.create_player(self.conn, "HTTP WR", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 5, "overall", verification_status="verified")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "Yahoo", 9, "overall", verification_status="verified")
        self.conn.close()  # release the file lock so the app's own connection can use it

        # app.py's routes call get_conn() with no args, which reads db.database's own
        # module-level DB_PATH internally — that's the actual value that needs patching.
        orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = self.db_path
        try:
            c = flask_app.app.test_client()
            r = c.get("/review")
            self.assertEqual(r.status_code, 200)

            conn2 = get_conn(self.db_path)
            review_id = conn2.execute(
                "SELECT review_id FROM review_items WHERE entity_type='ranking' AND status='pending'"
            ).fetchone()["review_id"]
            conn2.close()

            r = c.post(f"/review/{review_id}/resolve", data={"action": "rejected"}, follow_redirects=True)
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"trusted ranking value updated", r.data)

            conn3 = get_conn(self.db_path)
            trusted = conn3.execute("SELECT ranking_value FROM rankings WHERE player_id=? AND is_current=1", (pid,)).fetchone()
            conn3.close()
            self.assertEqual(trusted["ranking_value"], 5)
        finally:
            dbmod.DB_PATH = orig_db_path
            self.conn = get_conn(self.db_path)  # tearDown expects self.conn to be open


class TestRankingProvenance(RankingTestBase):
    def test_auto_accepted_ranking_has_full_provenance_chain(self):
        repo.create_player(self.conn, "Provenance WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Provenance WR", "position": "WR", "week_number": 1, "data_type": "ranking",
             "ranking_type": "overall", "value": 5, "confidence": 0.97},
        ])
        adapter = LocalFileSourceAdapter("Yahoo", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)

        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        source = self.conn.execute("SELECT name FROM sources WHERE source_id=?", (run["source_id"],)).fetchone()
        self.assertEqual(source["name"], "Yahoo")

        proposed = self.conn.execute("SELECT * FROM proposed_observations WHERE import_id=?", (result["import_id"],)).fetchone()
        self.assertEqual(proposed["ranking_type"], "overall")
        self.assertEqual(proposed["disposition"], "auto_accepted")

        trusted = self.conn.execute("SELECT * FROM rankings WHERE ranking_id=?", (proposed["resulting_record_id"],)).fetchone()
        self.assertEqual(trusted["ranking_value"], 5)
        self.assertEqual(trusted["provenance_ref"], f"import:{result['import_id']}")


if __name__ == "__main__":
    unittest.main()

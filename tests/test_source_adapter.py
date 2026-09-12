import unittest
import tempfile
import os
import json
from db.database import init_db, get_conn
from db import repository as repo
from fantasy import service as fsvc
from ingestion.collector import LocalFileSourceAdapter, AdapterUnavailable, SourceAdapter, NormalizedObservation
from ingestion import pipeline


def write_source_file(path, entries):
    with open(path, "w") as f:
        json.dump(entries, f)


class TestSourceAdapterFramework(unittest.TestCase):
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

    # --- happy path -------------------------------------------------

    def test_full_batch_auto_accepts_high_confidence_resolved_players(self):
        repo.create_player(self.conn, "Collector WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Collector WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receptions", "value": 7, "confidence": 0.99},
            {"player_name": "Collector WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 95, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertFalse(result["fetch_failed"])
        self.assertEqual(len(result["auto_accepted"]), 2)
        self.assertEqual(result["errors"], [])

        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        self.assertEqual(run["status"], "successful")

    # --- source outage ------------------------------------------------

    def test_source_outage_fails_run_cleanly_no_partial_data(self):
        adapter = LocalFileSourceAdapter("NFL", "/nonexistent/path/does_not_exist.json")
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertTrue(result["fetch_failed"])
        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        self.assertEqual(run["status"], "failed")
        # nothing was written
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM proposed_observations").fetchone()["c"], 0)

    def test_malformed_json_source_file_treated_as_outage(self):
        with open(self.src_path, "w") as f:
            f.write("{not valid json[[[")
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertTrue(result["fetch_failed"])

    def test_unknown_source_name_returns_config_error_gracefully(self):
        adapter = LocalFileSourceAdapter("NotARealSource", self.src_path)
        write_source_file(self.src_path, [])
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertTrue(result.get("config_error"))
        self.assertIsNone(result["collection_run_id"])  # no run row created for a bad config

    # --- malformed individual observations ----------------------------

    def test_malformed_observation_skipped_others_still_processed(self):
        repo.create_player(self.conn, "Good Player WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Good Player WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
            {"player_name": "", "position": "WR", "week_number": 1, "data_type": "actual",  # missing name
             "stat_name": "receptions", "value": 5, "confidence": 0.99},
            {"player_name": "Good Player WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "not_a_real_stat", "value": 5, "confidence": 0.99},  # unrecognized stat
            {"player_name": "Good Player WR", "position": "WR", "week_number": 1, "data_type": "bogus",  # bad data_type
             "stat_name": "receptions", "value": 5, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertFalse(result["fetch_failed"])
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(len(result["auto_accepted"]), 1)  # the one good observation still went through
        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        self.assertEqual(run["status"], "partial")

    def test_batch_with_only_malformed_observations_marks_run_failed(self):
        write_source_file(self.src_path, [
            {"player_name": "", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receptions", "value": 5, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        self.assertEqual(run["status"], "failed")

    # --- missing / ambiguous identity -----------------------------------

    def test_missing_player_routes_to_review(self):
        write_source_file(self.src_path, [
            {"player_name": "Nobody Knows This Guy", "position": "RB", "week_number": 1, "data_type": "actual",
             "stat_name": "rush_yards", "value": 60, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(result["auto_accepted"], [])
        self.assertEqual(len(result["sent_to_review"]), 1)
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        self.assertEqual(item["reason"], "player_not_found")

    def test_ambiguous_player_routes_to_review(self):
        repo.create_player(self.conn, "Dup Player", "RB")
        repo.create_player(self.conn, "Dup Player", "RB")
        write_source_file(self.src_path, [
            {"player_name": "Dup Player", "position": "RB", "week_number": 1, "data_type": "actual",
             "stat_name": "rush_yards", "value": 60, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        self.assertEqual(item["reason"], "ambiguous_identity")

    def test_collector_never_creates_a_player_on_its_own(self):
        write_source_file(self.src_path, [
            {"player_name": "Never Auto Created", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 60, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        exists = self.conn.execute("SELECT * FROM players WHERE display_name='Never Auto Created'").fetchone()
        self.assertIsNone(exists)  # only a human accepting the review item can create the player

    # --- duplicate collection -------------------------------------------

    def test_duplicate_auto_accepted_collection_is_idempotent(self):
        repo.create_player(self.conn, "Idempotent WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Idempotent WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)  # re-run, identical data
        rows = self.conn.execute("SELECT * FROM statistics WHERE stat_name='receiving_yards'").fetchall()
        self.assertEqual(len(rows), 1)  # no duplicate version created for an identical re-collected value

    def test_duplicate_pending_review_item_is_not_spammed(self):
        write_source_file(self.src_path, [
            {"player_name": "Unresolved Guy", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 60, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        r1 = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        r2 = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)  # scheduled re-run before review happens
        self.assertEqual(len(r1["sent_to_review"]), 1)
        self.assertEqual(len(r2["sent_to_review"]), 0)
        self.assertEqual(len(r2["duplicates_skipped"]), 1)
        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM review_items WHERE entity_type='proposed_observation' AND status='pending'"
        ).fetchone()["c"]
        self.assertEqual(pending, 1)  # not two

    # --- changed source data ---------------------------------------------

    def test_changed_value_on_recollection_creates_new_review_item(self):
        write_source_file(self.src_path, [
            {"player_name": "Changing Guy", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 60, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)

        write_source_file(self.src_path, [  # source corrected/updated its number
            {"player_name": "Changing Guy", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 65, "confidence": 0.99},
        ])
        r2 = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(r2["sent_to_review"]), 1)  # new value != old pending value -> not treated as duplicate
        self.assertEqual(r2["duplicates_skipped"], [])

    def test_changed_value_on_already_trusted_stat_triggers_existing_conflict_path(self):
        pid = repo.create_player(self.conn, "Trusted Then Changed WR", "WR")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receiving_yards", 60,
                            source_name="NFL", verification_status="verified")
        write_source_file(self.src_path, [
            {"player_name": "Trusted Then Changed WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 75, "confidence": 0.99},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(len(result["auto_accepted"]), 1)  # high confidence, resolved identity -> auto-accept path fires
        rows = self.conn.execute(
            "SELECT stat_value, is_current FROM statistics WHERE player_id=? ORDER BY statistic_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)  # old preserved, new trusted — same repo.add_statistic conflict machinery
        conflict = self.conn.execute("SELECT * FROM review_items WHERE entity_type='statistic' AND reason='conflict'").fetchone()
        self.assertIsNotNone(conflict)

    # --- provenance --------------------------------------------------------

    def test_full_provenance_chain_is_queryable(self):
        repo.create_player(self.conn, "Provenance WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Provenance WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 88, "confidence": 0.99, "team": "DET"},
        ])
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)

        # 1. Where did it come from? When was it collected?
        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        source = self.conn.execute("SELECT name FROM sources WHERE source_id=?", (run["source_id"],)).fetchone()
        self.assertEqual(source["name"], "NFL")
        self.assertIsNotNone(run["started_at"])
        self.assertIsNotNone(run["completed_at"])

        # 2. What did the source report, raw?
        raw = self.conn.execute("SELECT * FROM raw_observations WHERE collection_run_id=?", (result["collection_run_id"],)).fetchone()
        raw_payload = json.loads(raw["raw_content"])
        self.assertEqual(raw_payload[0]["value"], 88)

        # 3. What extraction/normalization happened, and was it auto-accepted or reviewed?
        proposed = self.conn.execute("SELECT * FROM proposed_observations WHERE import_id=?", (result["import_id"],)).fetchone()
        self.assertEqual(proposed["disposition"], "auto_accepted")
        self.assertEqual(proposed["field_confidence"], 0.99)

        # 4. What value is currently trusted, and why?
        trusted = self.conn.execute("SELECT * FROM statistics WHERE statistic_id=?", (proposed["resulting_record_id"],)).fetchone()
        self.assertEqual(trusted["stat_value"], 88)
        self.assertEqual(trusted["is_current"], 1)
        self.assertEqual(trusted["provenance_ref"], f"import:{result['import_id']}")

        # 5. Does it flow into the fantasy calculation?
        pid = self.conn.execute("SELECT player_id FROM players WHERE display_name='Provenance WR'").fetchone()["player_id"]
        fp = fsvc.compute_actual_fp(self.conn, pid, self.week1, "WR")
        self.assertAlmostEqual(fp.total_points, 8.8)  # 88 * 0.10 receiving yard points


if __name__ == "__main__":
    unittest.main()

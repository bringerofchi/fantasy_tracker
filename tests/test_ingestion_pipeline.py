import unittest
import tempfile
import os
from db.database import init_db, get_conn
from db import repository as repo
from fantasy import service as fsvc
from ingestion.extractor import ScreenshotExtraction, ExtractedField, ExtractorUnavailable, Extractor
from ingestion.fake_extractor import FakeExtractor
from ingestion import pipeline


def make_tmp_image(path):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"fake image bytes for test")


class TestIngestionPipeline(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.img_path = tempfile.mktemp(suffix=".png")
        make_tmp_image(self.img_path)

    def tearDown(self):
        self.conn.close()
        for p in (self.db_path, self.img_path):
            if os.path.exists(p):
                os.remove(p)

    def test_high_confidence_known_player_auto_accepts(self):
        repo.create_player(self.conn, "Amon-Ra St. Brown", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Amon-Ra St. Brown", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[
                ExtractedField("receptions", 8, 0.95),
                ExtractedField("receiving_yards", 110, 0.92),
                ExtractedField("receiving_tds", 1, 0.9),
                ExtractedField("rush_yards", 0, 0.9),
                ExtractedField("rush_tds", 0, 0.9),
                ExtractedField("fumbles_lost", 0, 0.9),
            ],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "test.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertFalse(result["extraction_failed"])
        self.assertEqual(len(result["auto_accepted"]), 6)
        self.assertEqual(len(result["sent_to_review"]), 0)

        week1 = self.conn.execute("SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)).fetchone()["week_id"]
        pid = self.conn.execute("SELECT player_id FROM players WHERE display_name='Amon-Ra St. Brown'").fetchone()["player_id"]
        fp = fsvc.compute_actual_fp(self.conn, pid, week1, "WR")
        self.assertTrue(fp.is_complete)
        self.assertAlmostEqual(fp.total_points, 8 + 11.0 + 6)

    def test_low_confidence_field_goes_to_review_not_auto_accepted(self):
        repo.create_player(self.conn, "Low Conf WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Low Conf WR", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 88, 0.4)],  # below threshold
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(result["auto_accepted"], [])
        self.assertEqual(len(result["sent_to_review"]), 1)
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        self.assertEqual(item["reason"], "low_confidence")
        self.assertEqual(item["confidence"], "Low")

    def test_unresolved_player_goes_to_review_even_with_high_confidence(self):
        # No player named this exists yet — high field confidence must NOT bypass identity resolution.
        extraction = ScreenshotExtraction(
            player_name_raw="Totally Unknown Player", player_name_confidence=0.99, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 88, 0.99)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(result["auto_accepted"], [])
        self.assertEqual(len(result["sent_to_review"]), 1)
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        self.assertEqual(item["reason"], "player_not_found")

    def test_ambiguous_identity_goes_to_review(self):
        repo.create_player(self.conn, "John Smith", "RB")
        repo.create_player(self.conn, "John Smith", "RB")
        extraction = ScreenshotExtraction(
            player_name_raw="John Smith", player_name_confidence=0.95, position_hint="RB", week_hint=1,
            fields=[ExtractedField("rush_yards", 80, 0.95)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(result["auto_accepted"], [])
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        self.assertEqual(item["reason"], "ambiguous_identity")

    def test_hard_invalid_value_goes_to_review_not_auto_accepted(self):
        repo.create_player(self.conn, "Bad Value RB", "RB")
        extraction = ScreenshotExtraction(
            player_name_raw="Bad Value RB", player_name_confidence=0.95, position_hint="RB", week_hint=1,
            fields=[ExtractedField("receptions", -3, 0.95)],  # OCR misread producing a negative count
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(result["auto_accepted"], [])
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='proposed_observation'").fetchone()
        self.assertEqual(item["reason"], "invalid_value")

    def test_implausible_value_flagged_for_review_despite_high_confidence(self):
        repo.create_player(self.conn, "Huge Game WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Huge Game WR", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 999, 0.97)],  # implausible — likely OCR error
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(result["auto_accepted"], [])
        item = self.conn.execute(
            "SELECT * FROM proposed_observations WHERE stat_name='receiving_yards'"
        ).fetchone()
        flags = item["validation_flags_json"]
        self.assertIn("plausible", flags)

    def test_week_mismatch_forces_review(self):
        repo.create_player(self.conn, "Week Mismatch WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Week Mismatch WR", player_name_confidence=0.95, position_hint="WR",
            week_hint=5,  # screenshot says week 5
            fields=[ExtractedField("receiving_yards", 80, 0.95)],
        )
        # user asserts week 1 at upload time — mismatch
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(result["auto_accepted"], [])

    def test_raw_upload_and_extraction_are_immutable_and_preserved_regardless_of_outcome(self):
        repo.create_player(self.conn, "Audit Trail WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Audit Trail WR", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 999, 0.97)],  # will be flagged, sent to review
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        import_id = result["import_id"]
        raw_rows = self.conn.execute("SELECT * FROM raw_observations WHERE import_id=?", (import_id,)).fetchall()
        # one row for the original file, one for the raw extraction JSON
        self.assertEqual(len(raw_rows), 2)
        content_types = {r["content_type"] for r in raw_rows}
        self.assertEqual(content_types, {"file", "ai_extraction_result"})
        # even after rejection, both raw rows and the proposed_observations row must remain
        proposed_id = result["sent_to_review"][0]
        pipeline.reject_proposed_observation(self.conn, proposed_id)
        raw_rows_after = self.conn.execute("SELECT * FROM raw_observations WHERE import_id=?", (import_id,)).fetchall()
        self.assertEqual(len(raw_rows_after), 2)
        prop = self.conn.execute("SELECT * FROM proposed_observations WHERE proposed_id=?", (proposed_id,)).fetchone()
        self.assertEqual(prop["disposition"], "rejected")

    def test_extractor_unavailable_routes_whole_import_to_review_not_a_crash(self):
        class BrokenExtractor(Extractor):
            def extract(self, image_path, position_hint=None):
                raise ExtractorUnavailable("no API key configured")

        result = pipeline.ingest_screenshot(self.conn, BrokenExtractor(), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertTrue(result["extraction_failed"])
        item = self.conn.execute("SELECT * FROM review_items WHERE entity_type='import'").fetchone()
        self.assertEqual(item["reason"], "source_problem")
        # the raw file upload itself is still recorded, even though extraction never ran
        raw = self.conn.execute("SELECT * FROM raw_observations WHERE import_id=?", (result["import_id"],)).fetchall()
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["content_type"], "file")

    def test_review_accept_with_correction_creates_trusted_statistic(self):
        repo.create_player(self.conn, "Correction WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Correction WR", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 88, 0.4)],  # low confidence -> review
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        proposed_id = result["sent_to_review"][0]
        # reviewer corrects 88 -> 91 (misread digit) and accepts
        new_id = pipeline.accept_proposed_observation(self.conn, proposed_id, corrected_value=91)
        trusted = self.conn.execute("SELECT stat_value, is_current FROM statistics WHERE statistic_id=?", (new_id,)).fetchone()
        self.assertEqual(trusted["stat_value"], 91)
        self.assertEqual(trusted["is_current"], 1)
        prop = self.conn.execute("SELECT disposition FROM proposed_observations WHERE proposed_id=?", (proposed_id,)).fetchone()
        self.assertEqual(prop["disposition"], "corrected_and_accepted")
        review_item = self.conn.execute("SELECT status FROM review_items WHERE entity_id=? AND entity_type='proposed_observation'", (proposed_id,)).fetchone()
        self.assertEqual(review_item["status"], "approved")

    def test_review_accept_with_new_player_creation(self):
        extraction = ScreenshotExtraction(
            player_name_raw="Brand New Rookie", player_name_confidence=0.95, position_hint="RB", week_hint=1,
            fields=[ExtractedField("rush_yards", 45, 0.9)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        proposed_id = result["sent_to_review"][0]  # player_not_found
        new_id = pipeline.accept_proposed_observation(self.conn, proposed_id, player_id_override="CREATE_NEW",
                                                        new_player_name="Brand New Rookie")
        new_player = self.conn.execute("SELECT * FROM players WHERE display_name='Brand New Rookie'").fetchone()
        self.assertIsNotNone(new_player)
        trusted = self.conn.execute("SELECT player_id FROM statistics WHERE statistic_id=?", (new_id,)).fetchone()
        self.assertEqual(trusted["player_id"], new_player["player_id"])

    def test_reject_creates_no_statistic(self):
        repo.create_player(self.conn, "Rejected WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Rejected WR", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 999, 0.97)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        proposed_id = result["sent_to_review"][0]
        pipeline.reject_proposed_observation(self.conn, proposed_id)
        count = self.conn.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_accepted_extraction_reuses_existing_conflict_detection(self):
        """An auto-accepted extracted stat that later conflicts with a manually-entered
        one must go through the SAME conflict/versioning path as any other source —
        no separate resolution system for AI-sourced conflicts."""
        pid = repo.create_player(self.conn, "Conflict WR", "WR")
        week1 = self.conn.execute("SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)).fetchone()["week_id"]
        repo.add_statistic(self.conn, pid, self.season_id, week1, "receiving_yards", 60, source_name="NFL", verification_status="verified")

        extraction = ScreenshotExtraction(
            player_name_raw="Conflict WR", player_name_confidence=0.95, position_hint="WR", week_hint=1,
            fields=[ExtractedField("receiving_yards", 75, 0.95)],  # high confidence, resolvable identity -> auto-accept
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=1, data_type="actual", source_name="NFL")
        self.assertEqual(len(result["auto_accepted"]), 1)

        # the SAME conflict machinery in db.repository fired: original preserved, new version trusted, flagged
        rows = self.conn.execute(
            "SELECT stat_value, is_current FROM statistics WHERE player_id=? ORDER BY statistic_id", (pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stat_value"], 60)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["stat_value"], 75)
        self.assertEqual(rows[1]["is_current"], 1)
        conflict_item = self.conn.execute(
            "SELECT * FROM review_items WHERE entity_type='statistic' AND reason='conflict'"
        ).fetchone()
        self.assertIsNotNone(conflict_item)

    def test_projection_data_type_writes_to_projections_table(self):
        repo.create_player(self.conn, "Proj WR", "WR")
        extraction = ScreenshotExtraction(
            player_name_raw="Proj WR", player_name_confidence=0.95, position_hint="WR", week_hint=2,
            fields=[ExtractedField("receiving_yards", 70, 0.9)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "t.png",
                                             self.season_id, week_number=2, data_type="projection", source_name="Yahoo")
        self.assertEqual(len(result["auto_accepted"]), 1)
        row = self.conn.execute("SELECT * FROM projections WHERE source_name='Yahoo' AND is_current=1").fetchone()
        self.assertEqual(row["projected_value"], 70)

    def test_unrecognized_stat_name_never_auto_accepts_even_at_high_confidence(self):
        """Regression test (2026-09-07): a real Athletic Fantasy Concierge screenshot
        surfaced a genuine integrity gap -- validate_stat_value only checked
        non-negativity for a known subset of stats and never checked whether
        stat_name was a real, recognized raw-stat category at all. A bare
        pre-computed 'fantasy_points' total, high confidence, exact identity
        match, previously auto-accepted and landed directly in the trusted
        `projections` table -- silently violating "raw stats only, points
        always derived, never stored". This test locks in the fix: an
        unrecognized stat_name must always be routed to the Review Queue with
        reason='invalid_value', regardless of how confident the extraction was,
        and must never create a projections/statistics row."""
        repo.create_player(self.conn, "Concierge RB", "RB")
        extraction = ScreenshotExtraction(
            player_name_raw="Concierge RB", player_name_confidence=0.97, position_hint="RB", week_hint=1,
            fields=[ExtractedField("fantasy_points", 21.4, 0.95)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "concierge.png",
                                             self.season_id, week_number=1, data_type="projection", source_name="The Athletic")
        self.assertFalse(result["extraction_failed"])
        self.assertEqual(len(result["auto_accepted"]), 0)
        self.assertEqual(len(result["sent_to_review"]), 1)

        # Never became a trusted projection.
        row = self.conn.execute(
            "SELECT * FROM projections WHERE source_name='The Athletic' AND stat_name='fantasy_points'"
        ).fetchone()
        self.assertIsNone(row)

        # The Review Queue entry explains why, not just that it failed.
        proposed_id = result["sent_to_review"][0]
        review_item = self.conn.execute(
            "SELECT * FROM review_items WHERE entity_type='proposed_observation' AND entity_id=?", (proposed_id,)
        ).fetchone()
        self.assertEqual(review_item["reason"], "invalid_value")
        self.assertIn("fantasy_points", review_item["details_json"])

    def test_recognized_stat_name_still_auto_accepts_after_the_fix(self):
        """Companion to the regression test above: confirms the fix is scoped to
        rejecting unrecognized stat_name values, not a general tightening that
        would also block legitimate raw stats. A real, known category at high
        confidence with a resolved identity must still auto-accept exactly as
        before."""
        repo.create_player(self.conn, "Legit RB", "RB")
        extraction = ScreenshotExtraction(
            player_name_raw="Legit RB", player_name_confidence=0.95, position_hint="RB", week_hint=1,
            fields=[ExtractedField("rush_yards", 81.9, 0.93)],
        )
        result = pipeline.ingest_screenshot(self.conn, FakeExtractor(extraction), self.img_path, "yahoo.png",
                                             self.season_id, week_number=1, data_type="projection", source_name="Yahoo")
        self.assertEqual(len(result["auto_accepted"]), 1)
        self.assertEqual(len(result["sent_to_review"]), 0)
        row = self.conn.execute(
            "SELECT * FROM projections WHERE source_name='Yahoo' AND stat_name='rush_yards' AND is_current=1"
        ).fetchone()
        self.assertEqual(row["projected_value"], 81.9)


if __name__ == "__main__":
    unittest.main()

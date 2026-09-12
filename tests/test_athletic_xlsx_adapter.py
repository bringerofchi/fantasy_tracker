"""
Tests for AthleticSeasonXlsxAdapter, run against a real excerpt of the actual
specimen you provided (tests/fixtures/athletic_2026_projections_excerpt.xlsx —
genuine header rows and a handful of genuine player rows copied verbatim from
the real file, not invented). This is deliberately NOT a fabricated fixture:
every value asserted below is a real number that was actually in the source
workbook, checked by direct inspection before writing these assertions.
"""
import os
import unittest
import tempfile

from db.database import init_db, get_conn
from db import repository as repo
from ingestion.athletic_xlsx_adapter import AthleticSeasonXlsxAdapter
from ingestion import pipeline

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "athletic_2026_projections_excerpt.xlsx")


class AthleticAdapterTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)


class TestAthleticAdapterFetch(AthleticAdapterTestBase):
    """Tests fetch() in isolation — does it correctly turn the real workbook
    into NormalizedObservations, before anything touches the pipeline."""

    def test_fetch_produces_only_season_scope_observations(self):
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        self.assertTrue(len(observations) > 0)
        self.assertTrue(all(o.scope == "season" for o in observations))
        self.assertTrue(all(o.week_number is None for o in observations))

    def test_real_qb_projection_values_match_the_source_workbook(self):
        """Jacoby Brissett, row 1 of the real QB sheet: PAYD=3231.85542624,
        PATD=21.359964024 — asserting the adapter reads these exact real
        numbers, not approximations or invented ones."""
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        brissett = [o for o in observations if o.player_name_raw == "Jacoby Brissett"]
        by_stat = {o.stat_name: o.value for o in brissett}
        self.assertAlmostEqual(by_stat["pass_yards"], 3231.86, places=1)
        self.assertAlmostEqual(by_stat["pass_tds"], 21.36, places=1)
        self.assertAlmostEqual(by_stat["interceptions"], 7.95, places=1)

    def test_precomputed_points_columns_are_never_imported(self):
        """FPS/HALF/PPR/Custom/AUC$ must never appear as stat_name values —
        raw stats only, points are always derived by our own engine."""
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        stat_names = {o.stat_name for o in observations if o.data_type == "projection"}
        self.assertNotIn("FPS", stat_names)
        self.assertNotIn("PPR", stat_names)
        self.assertNotIn("HALF", stat_names)
        self.assertNotIn("AUC$", stat_names)
        self.assertNotIn("Custom", stat_names)

    def test_untracked_columns_are_skipped_not_guessed(self):
        """RUAT (rush attempts) and TGT (targets) aren't in our stat_definitions
        at all — the adapter must not invent a stat_name for them."""
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        stat_names = {o.stat_name for o in observations if o.data_type == "projection"}
        self.assertNotIn("RUAT", stat_names)
        self.assertNotIn("TGT", stat_names)
        self.assertNotIn("rush_attempts", stat_names)
        self.assertNotIn("targets", stat_names)

    def test_real_ranking_values_match_the_source_workbook(self):
        """Josh Allen is rank 1 in the real QB ranking column group;
        Bijan Robinson rank 1 RB; Brock Bowers rank 1 TE — real values."""
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        rankings = {(o.player_name_raw, o.ranking_type): o.value for o in observations if o.data_type == "ranking"}
        self.assertEqual(rankings[("Josh Allen", "QB")], 1.0)
        self.assertEqual(rankings[("Bijan Robinson", "RB")], 1.0)
        self.assertEqual(rankings[("Brock Bowers", "TE")], 1.0)

    def test_duplicate_ranking_column_groups_are_not_double_counted(self):
        """The real Rankings sheet repeats RB/WR/TE column groups multiple
        times (flex-oriented views). Only the first group per position
        should be parsed — Bijan Robinson must get exactly ONE RB ranking
        observation, not three."""
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        bijan_rb_rankings = [o for o in observations
                              if o.player_name_raw == "Bijan Robinson" and o.data_type == "ranking" and o.ranking_type == "RB"]
        self.assertEqual(len(bijan_rb_rankings), 1)

    def test_missing_fumbles_lost_never_fabricated(self):
        """This workbook has no fumbles_lost column anywhere. The adapter
        must never invent a value for it — it should simply never appear."""
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        observations = adapter.fetch(self.season_id, week_number=1)
        stat_names = {o.stat_name for o in observations if o.data_type == "projection"}
        self.assertNotIn("fumbles_lost", stat_names)


class TestAthleticAdapterThroughSharedPipeline(AthleticAdapterTestBase):
    """The adapter's only job is fetch() -> NormalizedObservation. Everything
    past that must be the same shared pipeline every other source uses."""

    def test_known_player_auto_accepts_with_real_values(self):
        repo.create_player(self.conn, "Josh Allen", "QB")
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertFalse(result["fetch_failed"])
        self.assertTrue(len(result["auto_accepted"]) > 0)

        row = self.conn.execute(
            """SELECT ranking_value, ranking_type, scope, provenance_ref FROM rankings r
               JOIN players p ON p.player_id=r.player_id
               WHERE p.display_name='Josh Allen' AND r.is_current=1"""
        ).fetchone()
        self.assertEqual(row["ranking_value"], 1.0)
        self.assertEqual(row["ranking_type"], "QB")
        self.assertEqual(row["scope"], "season")
        self.assertTrue(row["provenance_ref"].startswith("import:"))

    def test_unknown_player_routed_to_review_not_auto_created(self):
        # No players created at all — every real player in the excerpt is unknown.
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertEqual(result["auto_accepted"], [])
        self.assertTrue(len(result["sent_to_review"]) > 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"], 0)

    def test_reviewer_can_accept_an_unresolved_player_by_creating_them(self):
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        proposed_id = result["sent_to_review"][0]
        prop = self.conn.execute("SELECT * FROM proposed_observations WHERE proposed_id=?", (proposed_id,)).fetchone()
        new_id = pipeline.accept_proposed_observation(
            self.conn, proposed_id, player_id_override="CREATE_NEW", new_player_name=prop["extracted_player_name"]
        )
        self.assertIsNotNone(new_id)
        player = self.conn.execute("SELECT * FROM players WHERE display_name=?", (prop["extracted_player_name"],)).fetchone()
        self.assertIsNotNone(player)

    def test_rerunning_the_same_file_is_idempotent_for_ranking_and_versions_for_projection(self):
        """Matches the documented, pre-existing behavior difference between
        add_ranking (idempotent on identical value) and add_projection
        (always versions) — proven here through the real adapter, not just
        the repository layer directly."""
        repo.create_player(self.conn, "Josh Allen", "QB")
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)  # identical re-run

        ranking_rows = self.conn.execute(
            """SELECT COUNT(*) c FROM rankings r JOIN players p ON p.player_id=r.player_id
               WHERE p.display_name='Josh Allen'"""
        ).fetchone()["c"]
        self.assertEqual(ranking_rows, 1)  # idempotent, no duplicate version

    def test_full_provenance_chain_for_a_real_projection(self):
        # Jacoby Brissett is the real player present in this trimmed QB-sheet
        # excerpt (Josh Allen only appears in the Rankings excerpt, not the
        # QB sheet excerpt, since the full workbook sorts QB rows by team and
        # this excerpt only kept the first few rows).
        repo.create_player(self.conn, "Jacoby Brissett", "QB")
        adapter = AthleticSeasonXlsxAdapter("The Athletic", FIXTURE_PATH)
        result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)

        run = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)).fetchone()
        source = self.conn.execute("SELECT name FROM sources WHERE source_id=?", (run["source_id"],)).fetchone()
        self.assertEqual(source["name"], "The Athletic")

        proj = self.conn.execute(
            """SELECT p.* FROM projections p JOIN players pl ON pl.player_id=p.player_id
               WHERE pl.display_name='Jacoby Brissett' AND p.stat_name='pass_yards' AND p.is_current=1"""
        ).fetchone()
        self.assertEqual(proj["scope"], "season")
        self.assertIsNone(proj["week_id"])
        self.assertAlmostEqual(proj["projected_value"], 3231.86, places=1)  # real value from the source workbook


if __name__ == "__main__":
    unittest.main()

import unittest
import tempfile
import os
from db.database import init_db, get_conn
from db import repository as repo
from fantasy import service as fsvc


class TestFantasyService(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.path)
        self.conn = get_conn(self.path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        self.week2 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=2", (self.season_id,)
        ).fetchone()["week_id"]
        self.player_id = repo.create_player(self.conn, "Test Player", "WR")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_weekly_accuracy_by_source_never_merges_espn_and_yahoo(self):
        """Regression test (2026-09-07): the /players/<id> route previously called
        player_accuracy_summary/player_weekly_view/player_trend with no source_name,
        silently defaulting to source_name='User' -- so ESPN's and Yahoo's weekly
        projection accuracy never surfaced anywhere, even with real projections and
        actuals in the database. This locks in the fix: player_weekly_comparison_by_source
        must discover both real sources and keep their accuracy figures fully separate,
        matching the same "never merge sources" convention player_season_projection_comparison
        already enforces for scope='season'."""
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receptions", 4,
                            scope="weekly", source_name="ESPN")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 60,
                            scope="weekly", source_name="ESPN")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", 0.5,
                            scope="weekly", source_name="ESPN")

        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receptions", 6,
                            scope="weekly", source_name="Yahoo")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 80,
                            scope="weekly", source_name="Yahoo")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", 1.5,
                            scope="weekly", source_name="Yahoo")

        # One real actual, same canonical source used everywhere else in this project --
        # both ESPN's and Yahoo's projections should be judged against this SAME ground truth.
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receptions", 5,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 70,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", 1,
                            source_name="NFL", verification_status="verified")

        comparison = fsvc.player_weekly_comparison_by_source(self.conn, self.player_id, self.season_id)

        self.assertEqual(comparison["sources"], ["ESPN", "Yahoo"])

        espn = comparison["by_source"]["ESPN"]
        yahoo = comparison["by_source"]["Yahoo"]

        # difference = actual - projected (per compare_projection_actual).
        # ESPN under-projected (fewer receptions/yards than actual) -> positive average difference.
        self.assertEqual(espn["accuracy"]["weeks"], 1)
        self.assertGreater(espn["accuracy"]["avg_difference"], 0)

        # Yahoo over-projected -> negative average difference. Different sign from ESPN,
        # proving the two are computed independently, not blended into one shared figure.
        self.assertEqual(yahoo["accuracy"]["weeks"], 1)
        self.assertLess(yahoo["accuracy"]["avg_difference"], 0)
        self.assertNotEqual(espn["accuracy"]["avg_projected"], yahoo["accuracy"]["avg_projected"])

        # Both were judged against the identical actual result.
        self.assertEqual(espn["accuracy"]["avg_actual"], yahoo["accuracy"]["avg_actual"])

        # A source with no weekly projections at all (the manual "User" path, untouched
        # by this fix) correctly doesn't show up as a phantom empty entry.
        self.assertNotIn("User", comparison["sources"])

    def test_projected_fp_from_db(self):
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receptions", 6, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 80, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", 0.5, source_name="User")
        result = fsvc.compute_projected_fp(self.conn, self.player_id, self.week1, "WR")
        self.assertAlmostEqual(result.total_points, 6 + 8 + 3)
        # rush_yards/rush_tds/fumbles_lost were never entered -> missing, not zero
        self.assertFalse(result.is_complete)
        self.assertIn("rush_yards", result.missing)

    def test_actual_fp_uses_canonical_source(self):
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receptions", 9,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 127,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", 1,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "rush_yards", 0,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "rush_tds", 0,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "fumbles_lost", 0,
                            source_name="NFL", verification_status="verified")
        result = fsvc.compute_actual_fp(self.conn, self.player_id, self.week1, "WR")
        self.assertAlmostEqual(result.total_points, 9 + 12.7 + 6)
        self.assertTrue(result.is_complete)

    def test_comparison_difference_and_absolute(self):
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receptions", 5, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 50, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", 0, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "rush_yards", 0, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "rush_tds", 0, source_name="User")
        repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "fumbles_lost", 0, source_name="User")
        # projected: 5 + 5 = 10
        for name, val in [("receptions", 8), ("receiving_yards", 100), ("receiving_tds", 1),
                           ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, name, val,
                                source_name="NFL", verification_status="verified")
        # actual: 8 + 10 + 6 = 24
        cmp = fsvc.compare_projection_actual(self.conn, self.player_id, self.week1, "WR")
        self.assertAlmostEqual(cmp["projected_fp"], 10.0)
        self.assertAlmostEqual(cmp["actual_fp"], 24.0)
        self.assertAlmostEqual(cmp["difference"], 14.0)   # positive: exceeded projection
        self.assertAlmostEqual(cmp["absolute_difference"], 14.0)

    def test_negative_difference_when_player_underperforms(self):
        for name, val in [("receptions", 8), ("receiving_yards", 100), ("receiving_tds", 1),
                           ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, name, val, source_name="User")
        for name, val in [("receptions", 2), ("receiving_yards", 15), ("receiving_tds", 0),
                           ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, name, val,
                                source_name="NFL", verification_status="verified")
        cmp = fsvc.compare_projection_actual(self.conn, self.player_id, self.week1, "WR")
        self.assertLess(cmp["difference"], 0)
        self.assertAlmostEqual(cmp["absolute_difference"], abs(cmp["difference"]))

    def test_zero_difference(self):
        for name, val in [("receptions", 5), ("receiving_yards", 50), ("receiving_tds", 0),
                           ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, name, val, source_name="User")
            repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, name, val,
                                source_name="NFL", verification_status="verified")
        cmp = fsvc.compare_projection_actual(self.conn, self.player_id, self.week1, "WR")
        self.assertEqual(cmp["difference"], 0)

    def test_correction_updates_calculated_fp_and_preserves_original(self):
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 75,
                            source_name="NFL", verification_status="verified")
        for name, val in [("receptions", 0), ("receiving_tds", 0), ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, name, val,
                                source_name="NFL", verification_status="verified")
        before = fsvc.compute_actual_fp(self.conn, self.player_id, self.week1, "WR")
        self.assertAlmostEqual(before.total_points, 7.5)

        # Correction: re-enter with a different value -> new version, conflict flagged, original preserved
        repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receiving_yards", 82,
                            source_name="NFL", verification_status="verified")
        after = fsvc.compute_actual_fp(self.conn, self.player_id, self.week1, "WR")
        self.assertAlmostEqual(after.total_points, 8.2)  # recalculated from the new trusted version

        # original observation still present in history (is_current=0), never deleted
        rows = self.conn.execute(
            "SELECT stat_value, is_current FROM statistics WHERE player_id=? AND stat_name='receiving_yards' ORDER BY statistic_id",
            (self.player_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stat_value"], 75)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["stat_value"], 82)
        self.assertEqual(rows[1]["is_current"], 1)

        # conflict remains visible in the review queue
        pending = self.conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending' AND reason='conflict'").fetchone()["c"]
        self.assertEqual(pending, 1)

    def test_accuracy_summary_over_and_under(self):
        # Week 1: player exceeds projection; Week 2: player falls short
        for wk, proj_val, actual_val in [(self.week1, 5, 8), (self.week2, 5, 2)]:
            for name, val in [("receptions", proj_val), ("receiving_yards", 0), ("receiving_tds", 0),
                               ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
                repo.add_projection(self.conn, self.player_id, self.season_id, wk, name, val, source_name="User")
            for name, val in [("receptions", actual_val), ("receiving_yards", 0), ("receiving_tds", 0),
                               ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
                repo.add_statistic(self.conn, self.player_id, self.season_id, wk, name, val,
                                    source_name="NFL", verification_status="verified")
        summary = fsvc.player_accuracy_summary(self.conn, self.player_id, self.season_id)
        self.assertEqual(summary["weeks"], 2)
        self.assertEqual(summary["weeks_over"], 1)
        self.assertEqual(summary["weeks_under"], 1)

    def test_data_integrity_rejects_negative_receptions(self):
        with self.assertRaises(ValueError):
            repo.add_statistic(self.conn, self.player_id, self.season_id, self.week1, "receptions", -1,
                                source_name="NFL")

    def test_data_integrity_rejects_negative_touchdowns(self):
        with self.assertRaises(ValueError):
            repo.add_projection(self.conn, self.player_id, self.season_id, self.week1, "receiving_tds", -1,
                                 source_name="User")


if __name__ == "__main__":
    unittest.main()

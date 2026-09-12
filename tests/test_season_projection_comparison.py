import unittest
import tempfile
import os
from db.database import init_db, get_conn
from db import repository as repo
from fantasy import service as fsvc


class TestSeasonProjectionComparison(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.pid = repo.create_player(self.conn, "Comparison QB", "QB")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_no_sources_returns_empty(self):
        result = fsvc.player_season_projection_comparison(self.conn, self.pid, self.season_id)
        self.assertEqual(result["sources"], [])

    def test_single_source_shows_up_correctly(self):
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200,
                             scope="season", source_name="ESPN")
        result = fsvc.player_season_projection_comparison(self.conn, self.pid, self.season_id)
        self.assertEqual(result["sources"], ["ESPN"])
        pass_yards_row = next(r for r in result["stat_rows"] if r["stat_name"] == "pass_yards")
        self.assertEqual(pass_yards_row["values"]["ESPN"], 4200)

    def test_multiple_sources_shown_side_by_side_not_merged(self):
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200,
                             scope="season", source_name="The Athletic")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4012,
                             scope="season", source_name="ESPN")
        result = fsvc.player_season_projection_comparison(self.conn, self.pid, self.season_id)
        self.assertEqual(sorted(result["sources"]), ["ESPN", "The Athletic"])
        pass_yards_row = next(r for r in result["stat_rows"] if r["stat_name"] == "pass_yards")
        self.assertEqual(pass_yards_row["values"]["The Athletic"], 4200)
        self.assertEqual(pass_yards_row["values"]["ESPN"], 4012)

    def test_missing_stat_from_one_source_shows_as_none_not_zero(self):
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200,
                             scope="season", source_name="The Athletic")
        # ESPN only provides pass_tds for this player, not pass_yards
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_tds", 24,
                             scope="season", source_name="ESPN")
        result = fsvc.player_season_projection_comparison(self.conn, self.pid, self.season_id)
        pass_yards_row = next(r for r in result["stat_rows"] if r["stat_name"] == "pass_yards")
        self.assertIsNone(pass_yards_row["values"]["ESPN"])  # missing, not fabricated as 0

    def test_totals_computed_independently_per_source(self):
        for stat, val in [("pass_yards", 4200), ("pass_tds", 27), ("interceptions", 11),
                           ("rush_yards", 538), ("rush_tds", 12), ("pass_completions", 346), ("pass_attempts", 516)]:
            repo.add_projection(self.conn, self.pid, self.season_id, None, stat, val,
                                 scope="season", source_name="The Athletic")
        for stat, val in [("pass_yards", 4012), ("pass_tds", 24), ("interceptions", 12),
                           ("rush_yards", 126), ("pass_completions", 335), ("pass_attempts", 512)]:
            repo.add_projection(self.conn, self.pid, self.season_id, None, stat, val,
                                 scope="season", source_name="ESPN")
        result = fsvc.player_season_projection_comparison(self.conn, self.pid, self.season_id)
        self.assertTrue(result["totals"]["The Athletic"]["is_complete"])
        self.assertFalse(result["totals"]["ESPN"]["is_complete"])  # ESPN missing rush_tds (truncated column, real limitation)
        self.assertIn("rush_tds", result["totals"]["ESPN"]["missing"])
        self.assertNotAlmostEqual(
            result["totals"]["The Athletic"]["total_points"], result["totals"]["ESPN"]["total_points"], places=1
        )

    def test_unknown_player_returns_empty_gracefully(self):
        result = fsvc.player_season_projection_comparison(self.conn, 99999, self.season_id)
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()

"""
Audit finding while building multi-source projection ingestion: unlike
add_statistic and add_ranking (both correctly scoped by source from the
start), add_projection's identity key never included source_name — so a
second source's independent projection for the same player/stat/week was
treated as a competing version of ONE projection rather than a separate
observation, with the second source silently "correcting" the first. That
directly defeats "compare projected vs actual for each site". Fixed by
adding source_name (via "IS ?" for the same NULL-safety reasons as week_id
and ranking_type before it) to the identity lookup.
"""
import unittest
import tempfile
import os
from db.database import init_db, get_conn
from db import repository as repo


class TestMultiSourceProjections(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.pid = repo.create_player(self.conn, "Multi Source QB", "QB")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_two_sources_season_projections_coexist(self):
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200,
                             scope="season", source_name="The Athletic")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4012,
                             scope="season", source_name="ESPN")
        rows = self.conn.execute(
            "SELECT source_name, projected_value FROM projections WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchall()
        by_source = {r["source_name"]: r["projected_value"] for r in rows}
        self.assertEqual(len(rows), 2)
        self.assertEqual(by_source["The Athletic"], 4200)
        self.assertEqual(by_source["ESPN"], 4012)

    def test_three_sources_season_projections_coexist(self):
        for source, value in [("The Athletic", 4200), ("ESPN", 4012), ("Yahoo", 4100)]:
            repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", value,
                                 scope="season", source_name=source)
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM projections WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchone()
        self.assertEqual(rows["c"], 3)

    def test_same_source_reentry_still_versions_correctly(self):
        """The fix must not break the existing single-source correction behavior —
        re-entering a value from the SAME source still versions/supersedes."""
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200,
                             scope="season", source_name="ESPN")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4012,
                             scope="season", source_name="ESPN")
        rows = self.conn.execute(
            "SELECT projected_value, is_current FROM projections WHERE player_id=? ORDER BY projection_id", (self.pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["is_current"], 1)
        self.assertEqual(rows[1]["projected_value"], 4012)

    def test_weekly_multi_source_projections_also_coexist(self):
        week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        repo.add_projection(self.conn, self.pid, self.season_id, week1, "pass_yards", 260,
                             scope="weekly", source_name="The Athletic")
        repo.add_projection(self.conn, self.pid, self.season_id, week1, "pass_yards", 245,
                             scope="weekly", source_name="ESPN")
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM projections WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchone()
        self.assertEqual(rows["c"], 2)

    def test_default_none_source_still_self_consistent(self):
        """A caller that never specifies source_name (defaults to None) must
        still be internally consistent — re-entry from that same "no source"
        caller versions correctly, same NULL-safety fix as week_id/ranking_type."""
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4300, scope="season")
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM projections WHERE player_id=?", (self.pid,)
        ).fetchone()
        self.assertEqual(rows["c"], 2)  # versioned, not two unrelated inserts
        current = self.conn.execute(
            "SELECT projected_value FROM projections WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchone()
        self.assertEqual(current["projected_value"], 4300)

    def test_fantasy_service_can_compute_per_source_projected_points(self):
        """Confirms the fantasy service layer (which already accepted a
        source_name parameter) actually resolves distinct per-source totals
        now that storage is fixed — this is the actual comparison capability
        the multi-source ingestion work exists to enable."""
        from fantasy import service as fsvc
        week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        for name, val in [("receptions", 5), ("receiving_yards", 60), ("receiving_tds", 0),
                           ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            pass
        wr = repo.create_player(self.conn, "Multi Source WR", "WR")
        athletic_stats = {"receptions": 6, "receiving_yards": 70, "receiving_tds": 1,
                           "rush_yards": 0, "rush_tds": 0, "fumbles_lost": 0}
        espn_stats = {"receptions": 5, "receiving_yards": 55, "receiving_tds": 0,
                      "rush_yards": 0, "rush_tds": 0, "fumbles_lost": 0}
        for stat, val in athletic_stats.items():
            repo.add_projection(self.conn, wr, self.season_id, week1, stat, val,
                                 scope="weekly", source_name="The Athletic")
        for stat, val in espn_stats.items():
            repo.add_projection(self.conn, wr, self.season_id, week1, stat, val,
                                 scope="weekly", source_name="ESPN")

        athletic_fp = fsvc.compute_projected_fp(self.conn, wr, week1, "WR", source_name="The Athletic")
        espn_fp = fsvc.compute_projected_fp(self.conn, wr, week1, "WR", source_name="ESPN")
        self.assertNotAlmostEqual(athletic_fp.total_points, espn_fp.total_points)
        self.assertTrue(athletic_fp.is_complete)
        self.assertTrue(espn_fp.is_complete)


if __name__ == "__main__":
    unittest.main()

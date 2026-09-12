import unittest
import tempfile
import os
from db.database import init_db, get_conn, normalize_name
from db import repository as repo


class TestRegressionExistingFunctionality(unittest.TestCase):
    """Confirms Phase 1/2 behavior (identity resolution, aliases, versioning,
    conflict detection, rankings, review queue) is unchanged by the fantasy layer."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.path)
        self.conn = get_conn(self.path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_identity_resolution_dedupes_name_variants(self):
        pid = repo.create_player(self.conn, "Ja'Marr Chase", "WR")
        found_id, match_type = repo.resolve_player(self.conn, "Jamarr Chase", "WR")
        self.assertEqual(found_id, pid)
        self.assertEqual(match_type, "exact")

    def test_identity_resolution_no_match_for_different_position(self):
        repo.create_player(self.conn, "Travis Kelce", "TE")
        found_id, match_type = repo.resolve_player(self.conn, "Travis Kelce", "WR")
        self.assertEqual(match_type, "none")

    def test_alias_resolution(self):
        # "Nuk" is a nickname alias that does NOT normalize to the canonical
        # name, so it can only be found via the alias table, not exact match.
        pid = repo.create_player(self.conn, "DeAndre Hopkins", "WR")
        self.conn.execute(
            "INSERT INTO player_name_aliases (player_id, alias, normalized_alias, alias_source) VALUES (?,?,?,?)",
            (pid, "Nuk Hopkins", normalize_name("Nuk Hopkins"), "manual"),
        )
        self.conn.commit()
        found_id, match_type = repo.resolve_player(self.conn, "Nuk Hopkins", "WR")
        self.assertEqual(found_id, pid)
        self.assertEqual(match_type, "alias")

    def test_statistic_versioning_preserves_history(self):
        pid = repo.create_player(self.conn, "Test RB", "RB")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "rush_yards", 80, source_name="NFL")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "rush_yards", 95, source_name="NFL")
        rows = self.conn.execute(
            "SELECT stat_value, observation_version, is_current FROM statistics WHERE player_id=? ORDER BY statistic_id",
            (pid,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["is_current"], 1)
        self.assertEqual(rows[1]["observation_version"], 2)

    def test_conflict_flagged_for_review(self):
        pid = repo.create_player(self.conn, "Test WR2", "WR")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receptions", 5, source_name="NFL")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receptions", 7, source_name="NFL")
        items = self.conn.execute("SELECT * FROM review_items WHERE status='pending'").fetchall()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason"], "conflict")

    def test_idempotent_reentry_does_not_create_duplicate_or_review_item(self):
        pid = repo.create_player(self.conn, "Test TE2", "TE")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receptions", 5, source_name="NFL")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receptions", 5, source_name="NFL")
        rows = self.conn.execute("SELECT * FROM statistics WHERE player_id=?", (pid,)).fetchall()
        self.assertEqual(len(rows), 1)
        items = self.conn.execute("SELECT * FROM review_items").fetchall()
        self.assertEqual(len(items), 0)

    def test_ranking_entry_and_retrieval(self):
        pid = repo.create_player(self.conn, "Test WR3", "WR")
        repo.add_ranking(self.conn, pid, self.season_id, self.week1, "ESPN", 3, "WR", source_type="manual")
        rows = self.conn.execute("SELECT * FROM rankings WHERE player_id=? AND is_current=1", (pid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ranking_value"], 3)

    def test_ambiguous_match_returns_ambiguous_not_a_guess(self):
        # Two players end up with the same normalized name/position (edge case,
        # simulating a prior bad import) -> resolver must not silently pick one.
        repo.create_player(self.conn, "John Smith", "RB")
        repo.create_player(self.conn, "John Smith", "RB")
        found_id, match_type = repo.resolve_player(self.conn, "John Smith", "RB")
        self.assertIsNone(found_id)
        self.assertEqual(match_type, "ambiguous")


if __name__ == "__main__":
    unittest.main()

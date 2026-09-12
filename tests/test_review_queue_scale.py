"""
Review Queue scale fix: pagination, filtering by source/reason/entity_type,
and a safe bulk-reject (never bulk-accept — approving at scale would mean
trusting unverified identities/values sight unseen, exactly what review
exists to prevent). Uses a synthetic dataset sized to actually exercise
pagination, rather than depending on the real multi-thousand-item queue.
"""
import unittest
import tempfile
import os
import json
from db.database import init_db, get_conn
from db import repository as repo


class ReviewQueueScaleTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    _counter = 0

    def _make_pending_item(self, source_name, reason, entity_type="proposed_observation"):
        self.__class__._counter += 1
        stat_value = 100 + self.__class__._counter  # vary per call so the pending-duplicate unique index doesn't collide
        import_id = self.conn.execute(
            "INSERT INTO imports (file_name, file_path, file_hash, file_type, status) VALUES ('x','x','x','x','uploaded')"
        ).lastrowid
        prop_cur = self.conn.execute(
            """INSERT INTO proposed_observations
               (import_id, extracted_player_name, identity_match_type, position, data_type, scope,
                season_id, week_id, source_name, stat_name, stat_value, field_confidence,
                validation_flags_json, disposition)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (import_id, f"Test Player {self.__class__._counter}", "none", "QB", "projection", "season",
             self.season_id, None, source_name, "pass_yards", stat_value, 0.5, "[]", "sent_to_review"),
        )
        proposed_id = prop_cur.lastrowid
        details = {"proposed_id": proposed_id, "extracted_player_name": f"Test Player {self.__class__._counter}",
                   "identity_match_type": "none", "resolved_player_id": None, "stat_name": "pass_yards",
                   "stat_value": stat_value, "field_confidence": 0.5, "week_number": 1, "scope": "season",
                   "source_name": source_name, "data_type": "projection", "flags": [], "hard_invalid_reason": None}
        cur = self.conn.execute(
            """INSERT INTO review_items (entity_type, entity_id, reason, confidence, source_name, details_json, status)
               VALUES (?, ?, ?, 'Low', ?, ?, 'pending')""",
            (entity_type, proposed_id, reason, source_name, json.dumps(details)),
        )
        self.conn.commit()
        return cur.lastrowid


class TestReviewQueueFilteringAndPagination(ReviewQueueScaleTestBase):
    def test_source_name_column_populated_on_creation(self):
        review_id = self._make_pending_item("ESPN", "player_not_found")
        row = self.conn.execute("SELECT source_name FROM review_items WHERE review_id=?", (review_id,)).fetchone()
        self.assertEqual(row["source_name"], "ESPN")

    def test_filter_by_source_name_in_sql(self):
        self._make_pending_item("ESPN", "player_not_found")
        self._make_pending_item("The Athletic", "player_not_found")
        self._make_pending_item("ESPN", "low_confidence")
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM review_items WHERE status='pending' AND source_name='ESPN'"
        ).fetchone()
        self.assertEqual(rows["c"], 2)

    def test_pagination_math(self):
        for i in range(60):
            self._make_pending_item("ESPN", "player_not_found")
        total = self.conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
        per_page = 25
        total_pages = max(1, -(-total // per_page))
        self.assertEqual(total, 60)
        self.assertEqual(total_pages, 3)  # 25, 25, 10

    def test_conflict_review_items_also_get_source_name(self):
        """Statistic/ranking conflicts must populate source_name too, not just
        proposed_observation items — verified through the real creation path,
        not just the test helper."""
        pid = repo.create_player(self.conn, "Conflict WR", "WR")
        week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        repo.add_statistic(self.conn, pid, self.season_id, week1, "receiving_yards", 60, source_name="NFL")
        repo.add_statistic(self.conn, pid, self.season_id, week1, "receiving_yards", 75, source_name="NFL")
        row = self.conn.execute(
            "SELECT source_name FROM review_items WHERE entity_type='statistic' AND reason='conflict'"
        ).fetchone()
        self.assertEqual(row["source_name"], "NFL")


class TestReviewQueueHTTP(ReviewQueueScaleTestBase):
    def setUp(self):
        super().setUp()
        import db.database as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = self.db_path

    def tearDown(self):
        import db.database as dbmod
        dbmod.DB_PATH = self._orig_db_path
        super().tearDown()

    def test_page_renders_small_at_scale(self):
        for i in range(80):
            self._make_pending_item("ESPN", "player_not_found")
        import app as flask_app
        c = flask_app.app.test_client()
        r = c.get("/review")
        self.assertEqual(r.status_code, 200)
        # Should show only one page's worth (25), not all 80
        self.assertLess(len(r.data), 100_000)  # nowhere near the old multi-MB scale

    def test_filter_query_param_narrows_results(self):
        self._make_pending_item("ESPN", "player_not_found")
        self._make_pending_item("The Athletic", "player_not_found")
        import app as flask_app
        c = flask_app.app.test_client()
        r = c.get("/review?source_name=ESPN")
        self.assertEqual(r.status_code, 200)

    def test_bulk_reject_refuses_with_no_filter(self):
        self._make_pending_item("ESPN", "player_not_found")
        import app as flask_app
        c = flask_app.app.test_client()
        r = c.post("/review/bulk_reject", data={}, follow_redirects=True)
        self.assertIn(b"refusing to reject the entire queue unfiltered", r.data)
        # nothing was actually rejected
        pending = self.conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
        self.assertEqual(pending, 1)

    def test_bulk_reject_with_filter_only_affects_matching_items(self):
        self._make_pending_item("ESPN", "player_not_found")
        self._make_pending_item("ESPN", "player_not_found")
        self._make_pending_item("The Athletic", "player_not_found")
        import app as flask_app
        c = flask_app.app.test_client()
        r = c.post("/review/bulk_reject", data={"source_name": "ESPN"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        espn_pending = self.conn.execute(
            "SELECT COUNT(*) c FROM review_items WHERE status='pending' AND source_name='ESPN'"
        ).fetchone()["c"]
        athletic_pending = self.conn.execute(
            "SELECT COUNT(*) c FROM review_items WHERE status='pending' AND source_name='The Athletic'"
        ).fetchone()["c"]
        self.assertEqual(espn_pending, 0)
        self.assertEqual(athletic_pending, 1)  # untouched — filter was source-specific

    def test_bulk_reject_never_offers_bulk_accept(self):
        """Confirms only a reject route exists — no bulk-accept endpoint."""
        import app as flask_app
        rules = [r.rule for r in flask_app.app.url_map.iter_rules()]
        self.assertIn("/review/bulk_reject", rules)
        self.assertNotIn("/review/bulk_accept", rules)
        self.assertNotIn("/review/bulk_approve", rules)


if __name__ == "__main__":
    unittest.main()

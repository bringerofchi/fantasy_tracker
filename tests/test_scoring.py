import unittest
from fantasy.scoring import calculate_fantasy_points, PPR_SCORING


class TestScoringEngine(unittest.TestCase):
    def test_qb_passing_and_rushing(self):
        stats = {"pass_yards": 300, "pass_tds": 2, "interceptions": 1,
                  "rush_yards": 20, "rush_tds": 1, "pass_completions": 22, "pass_attempts": 25}
        r = calculate_fantasy_points(stats)
        # 300*0.04=12, 2*4=8, 1*-2=-2, 20*0.10=2, 1*6=6 => 26.0
        self.assertAlmostEqual(r.total_points, 26.0)
        self.assertTrue(r.is_complete)
        self.assertNotIn("pass_completions", r.breakdown)  # not a scorable stat
        self.assertNotIn("pass_attempts", r.breakdown)

    def test_qb_zero_rushing_is_zero_not_missing(self):
        stats = {"pass_yards": 250, "pass_tds": 1, "interceptions": 0,
                  "rush_yards": 0, "rush_tds": 0, "pass_completions": 18, "pass_attempts": 22}
        r = calculate_fantasy_points(stats)
        self.assertAlmostEqual(r.total_points, 250 * 0.04 + 4)
        self.assertTrue(r.is_complete)
        self.assertEqual(r.missing, [])

    def test_rb_scoring(self):
        stats = {"rush_yards": 100, "rush_tds": 1, "receptions": 4,
                  "receiving_yards": 30, "receiving_tds": 0, "fumbles_lost": 0}
        r = calculate_fantasy_points(stats)
        # 100*.10=10, 6, 4*1=4, 30*.10=3, 0, 0 => 23.0
        self.assertAlmostEqual(r.total_points, 23.0)

    def test_wr_scoring_with_no_passing_stats(self):
        stats = {"receptions": 7, "receiving_yards": 95, "receiving_tds": 1,
                  "rush_yards": 0, "rush_tds": 0, "fumbles_lost": 0}
        r = calculate_fantasy_points(stats)
        # 7 + 9.5 + 6 = 22.5
        self.assertAlmostEqual(r.total_points, 22.5)

    def test_te_scoring(self):
        stats = {"receptions": 5, "receiving_yards": 60, "receiving_tds": 1, "fumbles_lost": 0}
        r = calculate_fantasy_points(stats)
        self.assertAlmostEqual(r.total_points, 5 + 6 + 6)

    def test_fumble_lost_deduction(self):
        stats = {"receptions": 3, "receiving_yards": 20, "receiving_tds": 0, "fumbles_lost": 1}
        r = calculate_fantasy_points(stats)
        self.assertAlmostEqual(r.total_points, 3 + 2.0 - 2)

    def test_fractional_yardage(self):
        stats = {"receptions": 1, "receiving_yards": 12.5, "receiving_tds": 0, "fumbles_lost": 0}
        r = calculate_fantasy_points(stats)
        self.assertAlmostEqual(r.total_points, 1 + 1.25)

    def test_missing_stat_does_not_become_zero_points(self):
        stats = {"receptions": 5, "receiving_yards": 60, "receiving_tds": None, "fumbles_lost": 0}
        r = calculate_fantasy_points(stats)
        self.assertFalse(r.is_complete)
        self.assertIn("receiving_tds", r.missing)
        self.assertNotIn("receiving_tds", r.breakdown)
        self.assertAlmostEqual(r.total_points, 5 + 6.0)  # TD not counted as 0-contributing entry, just absent

    def test_all_missing(self):
        stats = {"receptions": None, "receiving_yards": None, "receiving_tds": None, "fumbles_lost": None}
        r = calculate_fantasy_points(stats)
        self.assertEqual(r.total_points, 0.0)
        self.assertFalse(r.is_complete)
        self.assertEqual(len(r.missing), 4)

    def test_custom_scoring_config_not_hardcoded(self):
        half_ppr = dict(PPR_SCORING)
        half_ppr["reception_points"] = 0.5
        stats = {"receptions": 10, "receiving_yards": 0, "receiving_tds": 0, "fumbles_lost": 0}
        r = calculate_fantasy_points(stats, scoring_config=half_ppr)
        self.assertAlmostEqual(r.total_points, 5.0)


if __name__ == "__main__":
    unittest.main()

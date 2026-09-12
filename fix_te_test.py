"""
One-shot fix for tests/test_phase2_audit.py's TE stat-set assertion,
which still expects the pre-rush_attempts stat set. Only patches if it
finds exactly the text it expects.
"""
path = "tests/test_phase2_audit.py"

old = '''    def test_te_has_no_passing_or_rushing_fields_expected(self):
        pid = repo.create_player(self.conn, "Blocking TE", "TE")
        defs = fsvc.get_stat_names_for_position(self.conn, "TE")
        self.assertEqual(set(defs), {"receptions", "receiving_yards", "receiving_tds", "fumbles_lost"})'''

new = '''    def test_te_has_no_passing_or_rushing_yardage_fields_expected(self):
        # TE gained rush_attempts (informational-only, ESPN weekly-live
        # adapter integration) without gaining rush_yards/rush_tds -- TEs
        # essentially never rush for yardage, but the raw attempt count
        # is still a real category the source can report.
        pid = repo.create_player(self.conn, "Blocking TE", "TE")
        defs = fsvc.get_stat_names_for_position(self.conn, "TE")
        self.assertEqual(set(defs), {"receptions", "receiving_yards", "receiving_tds",
                                      "rush_attempts", "fumbles_lost"})
        self.assertNotIn("pass_yards", defs)
        self.assertNotIn("rush_yards", defs)
        self.assertNotIn("rush_tds", defs)'''

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

if new.split("\n")[0] in content or "rush_attempts" in content.split("test_te_has_no_passing")[1][:500]:
    print("[SKIP] already applied.")
elif old not in content:
    print("[SKIP] expected text not found -- this file may have diverged from what I expect.")
    print("No changes made. Paste me the current test_te_has_no_passing_or_rushing_fields_expected function.")
else:
    content = content.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[OK] applied and saved.")

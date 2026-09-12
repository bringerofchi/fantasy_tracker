"""
Tests for the ESPN weekly live adapter, against a real captured specimen
(tests/fixtures/espn_weekly_live_qb_rb_wr_te_2025_2026_raw.json -- the
same real ESPN response used to establish the stat-ID mapping in
espn_weekly_stat_map.py, not fabricated).

fetch() itself makes a live HTTP call, so these tests exercise the
parsing seam (espn_weekly_core.parse_player_entry_weekly) directly
against the fixture's raw player entries -- the same separation the
standalone espn_adapter/ project uses between its live adapter and its
LocalFileSourceAdapter-tested core parser. A live network check for
ESPNWeeklyLiveAdapter.fetch() itself lives in
scripts/live_check_espn_weekly.py, guarded and outside pytest.
"""
import json
import os
import tempfile
import unittest

from db.database import init_db, get_conn
from ingestion import pipeline
from ingestion.collector import AdapterUnavailable
from ingestion.espn_weekly_core import parse_player_entry_weekly
from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter
from ingestion.espn_weekly_stat_map import STAT_ID_TO_NAME

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "espn_weekly_live_qb_rb_wr_te_2025_2026_raw.json"
)


def _load_players(season_key="season2025"):
    with open(FIXTURE_PATH) as f:
        data = json.load(f)
    return data[season_key]["players"]


def _entry_for(players, full_name):
    for entry in players:
        if entry["player"]["fullName"] == full_name:
            return entry
    raise AssertionError(f"{full_name!r} not found in fixture")


class TestParsePlayerEntryWeeklyKnownValues(unittest.TestCase):
    """Every value asserted here was independently verified against a real
    official box score before being written -- see espn_weekly_stat_map.py's
    evidence log for the exact sources."""

    def setUp(self):
        self.players = _load_players("season2025")

    def test_ja_marr_chase_week5_actual_matches_box_score(self):
        entry = _entry_for(self.players, "Ja'Marr Chase")
        obs = parse_player_entry_weekly(entry, season_id=2025, week_number=5, confidence=0.85)
        actuals = {o.stat_name: o.value for o in obs if o.data_type == "actual"}
        # ESPN box score, real game: 6 REC, 110 YDS, 2 TD
        self.assertEqual(actuals["receptions"], 6)
        self.assertEqual(actuals["receiving_yards"], 110)
        self.assertEqual(actuals["receiving_tds"], 2)

    def test_jahmyr_gibbs_week5_actual_matches_box_score(self):
        entry = _entry_for(self.players, "Jahmyr Gibbs")
        obs = parse_player_entry_weekly(entry, season_id=2025, week_number=5, confidence=0.85)
        actuals = {o.stat_name: o.value for o in obs if o.data_type == "actual"}
        self.assertEqual(actuals["rush_attempts"], 12)
        self.assertEqual(actuals["rush_yards"], 54)
        self.assertEqual(actuals["receptions"], 2)
        self.assertEqual(actuals["receiving_yards"], 33)
        self.assertEqual(actuals["receiving_tds"], 1)
        self.assertNotIn("rush_tds", actuals)  # 0 rushing TDs that week -- ESPN omits, not a 0 value

    def test_jahmyr_gibbs_week12_actual_matches_ap_recap(self):
        entry = _entry_for(self.players, "Jahmyr Gibbs")
        obs = parse_player_entry_weekly(entry, season_id=2025, week_number=12, confidence=0.85)
        actuals = {o.stat_name: o.value for o in obs if o.data_type == "actual"}
        # AP recap: 15 carries, 219 rush yards, 2 rush TD, 11 catches, 45 rec yards, 1 rec TD
        self.assertEqual(actuals["rush_attempts"], 15)
        self.assertEqual(actuals["rush_yards"], 219)
        self.assertEqual(actuals["rush_tds"], 2)
        self.assertEqual(actuals["receptions"], 11)
        self.assertEqual(actuals["receiving_yards"], 45)
        self.assertEqual(actuals["receiving_tds"], 1)

    def test_josh_allen_week5_actual_matches_pfr_box_score(self):
        entry = _entry_for(self.players, "Josh Allen")
        obs = parse_player_entry_weekly(entry, season_id=2025, week_number=5, confidence=0.85)
        actuals = {o.stat_name: o.value for o in obs if o.data_type == "actual"}
        # PFR box score: 22/31, 253 yds, 2 TD, 1 INT, 9 rush att, 53 rush yds
        self.assertEqual(actuals["pass_attempts"], 31)
        self.assertEqual(actuals["pass_completions"], 22)
        self.assertEqual(actuals["pass_yards"], 253)
        self.assertEqual(actuals["pass_tds"], 2)
        self.assertEqual(actuals["interceptions"], 1)
        self.assertEqual(actuals["rush_attempts"], 9)
        self.assertEqual(actuals["rush_yards"], 53)

    def test_trey_mcbride_produces_only_receiving_categories(self):
        entry = _entry_for(self.players, "Trey McBride")
        obs = parse_player_entry_weekly(entry, season_id=2025, week_number=5, confidence=0.85)
        stat_names = {o.stat_name for o in obs}
        # TE with no rushing that week -- only receiving categories should appear
        self.assertTrue(stat_names.issubset({"receptions", "receiving_yards", "receiving_tds"}))
        self.assertTrue(len(stat_names) > 0)


class TestAppliedTotalNeverEmitted(unittest.TestCase):
    """This project's core invariant: raw stats only, fantasy points always
    derived downstream, never stored. appliedTotal must never surface as
    an observation regardless of which player/week is parsed."""

    def test_no_observation_ever_uses_applied_total_as_stat_name(self):
        players = _load_players("season2025")
        for entry in players:
            for week in range(1, 19):
                try:
                    obs = parse_player_entry_weekly(entry, season_id=2025, week_number=week, confidence=0.85)
                except Exception:
                    continue
                for o in obs:
                    self.assertNotEqual(o.stat_name, "appliedTotal")
                    self.assertIn(o.stat_name, STAT_ID_TO_NAME.values())

    def test_mapped_ids_never_include_applied_total_key(self):
        self.assertNotIn("appliedTotal", STAT_ID_TO_NAME)


class TestWeekNumberAndAbsence(unittest.TestCase):
    def setUp(self):
        self.players = _load_players("season2025")

    def test_future_or_unplayed_week_produces_no_actual_observation(self):
        entry = _entry_for(self.players, "Jahmyr Gibbs")
        # week 8 is Gibbs's documented bye week in this dataset (projected
        # total of 0, per FINDINGS.md) -- confirm no fabricated actuals.
        obs = parse_player_entry_weekly(entry, season_id=2025, week_number=8, confidence=0.85)
        actuals = [o for o in obs if o.data_type == "actual"]
        self.assertEqual(actuals, [])

    def test_week_number_must_be_positive(self):
        from ingestion.espn_weekly_core import find_weekly_stat_entry
        entry = _entry_for(self.players, "Josh Allen")
        with self.assertRaises(ValueError):
            find_weekly_stat_entry(entry, season_id=2025, week_number=0, stat_source_id=0)


class TestIngestionPipelineIntegration(unittest.TestCase):
    """Confirms the new adapter's output actually lands in the real
    weekly-scope statistics/projections tables through the same
    ingest_collector_batch path the season-PDF adapter uses -- proving
    wiring, not just parsing correctness."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_parsed_observations_ingest_as_weekly_scope_rows(self):
        players = _load_players("season2025")
        entry = _entry_for(players, "Ja'Marr Chase")
        observations = parse_player_entry_weekly(entry, season_id=2025, week_number=5, confidence=0.85)
        self.assertTrue(len(observations) > 0)

        class _FixtureAdapter:
            source_name = "ESPN"

            def fetch(self, season_id, week_number):
                return observations

        result = pipeline.ingest_collector_batch(self.conn, _FixtureAdapter(), self.season_id, week_number=5)
        self.assertFalse(result.get("fetch_failed"))
        self.assertTrue(len(result["auto_accepted"]) + len(result["sent_to_review"]) > 0)

        rows = self.conn.execute(
            "SELECT stat_name, stat_value FROM statistics WHERE week_id IS NOT NULL"
        ).fetchall()
        row_names = {r["stat_name"] for r in rows}
        # whatever landed as trusted statistics must be real raw categories,
        # never a stray appliedTotal
        self.assertNotIn("appliedTotal", row_names)


class TestRushAttemptsRegistered(unittest.TestCase):
    """rush_attempts was added to stat_definitions (db/database.py
    STAT_DEFINITIONS) specifically so this adapter's raw output isn't
    silently dropped by the pipeline's known-stat-name check. Confirms
    it's registered for QB/RB/WR/TE (informational-only, same treatment
    as pass_attempts) and that it never lands in fantasy/scoring.py's
    scored-stats mapping."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_rush_attempts_registered_for_all_four_positions(self):
        for position in ("QB", "RB", "WR", "TE"):
            rows = self.conn.execute(
                "SELECT 1 FROM stat_definitions WHERE position=? AND stat_name='rush_attempts'",
                (position,),
            ).fetchall()
            self.assertEqual(len(rows), 1, f"rush_attempts missing from stat_definitions for {position}")

    def test_rush_attempts_is_never_a_scored_stat(self):
        from fantasy.scoring import STAT_TO_SCORING_KEY
        self.assertNotIn("rush_attempts", STAT_TO_SCORING_KEY)


class TestFetchWrapsHttpFailuresAsAdapterUnavailable(unittest.TestCase):
    """Confirms the app's error contract (AdapterUnavailable), not ESPN-
    specific exception types, is what escapes fetch() -- required for
    ingest_collector_batch's existing failure handling to work unchanged."""

    def test_unreachable_host_raises_adapter_unavailable(self):
        adapter = ESPNWeeklyLiveAdapter(source_name="ESPN", player_ids=[1])
        # Point at a host that cannot resolve, forcing a ConnectionError
        # inside _fetch_raw, to prove the translation without needing a
        # live ESPN call.
        import ingestion.espn_weekly_live_adapter as mod
        original = mod.ESPN_ENDPOINT
        mod.ESPN_ENDPOINT = "https://this-host-does-not-exist.invalid/{season}"
        try:
            with self.assertRaises(AdapterUnavailable):
                adapter.fetch(season_id=2025, week_number=5)
        finally:
            mod.ESPN_ENDPOINT = original


if __name__ == "__main__":
    unittest.main()

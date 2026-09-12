"""
Manual live-network smoke check for ESPNWeeklyLiveAdapter.

Deliberately NOT a pytest test and NOT named *_test.py -- see
pytest.ini and espn_adapter/live_test.py's docstring for why that glob
collision is a real, previously-hit bug in this project. Run this
directly:

    python3 scripts/live_check_espn_weekly.py <season_id> <week_number> [player_id ...]

Example (Jahmyr Gibbs, week 5, 2025 -- the same specimen the stat-ID
mapping evidence pass used):

    python3 scripts/live_check_espn_weekly.py 2025 5 4429795

Needs real internet access; this project's sandbox cannot reach
lm-api-reads.fantasy.espn.com (egress allowlist), same limitation
documented for the standalone espn_adapter/ project. Run from a normal
machine.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter
from ingestion.collector import AdapterUnavailable


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 live_check_espn_weekly.py <season_id> <week_number> [player_id ...]")
        sys.exit(1)

    season_id = int(sys.argv[1])
    week_number = int(sys.argv[2])
    player_ids = [int(p) for p in sys.argv[3:]] if len(sys.argv) > 3 else None

    adapter = ESPNWeeklyLiveAdapter(source_name="ESPN", player_ids=player_ids)
    try:
        observations = adapter.fetch(season_id, week_number)
    except AdapterUnavailable as e:
        print(f"[FAIL] AdapterUnavailable: {e} (transient={e.transient})")
        sys.exit(1)

    print(f"[OK] fetch() returned {len(observations)} observations")
    by_type = {}
    for o in observations:
        by_type.setdefault(o.data_type, 0)
        by_type[o.data_type] += 1
    print(f"By data_type: {by_type}")

    for o in observations[:20]:
        print(f"  {o.player_name_raw:25s} {o.data_type:10s} week={o.week_number:2d} "
              f"{o.stat_name:20s} = {o.value}")

    applied_total_leak = [o for o in observations if o.stat_name == "appliedTotal"]
    if applied_total_leak:
        print(f"[FAIL] {len(applied_total_leak)} observation(s) used 'appliedTotal' as stat_name -- invariant violated!")
        sys.exit(1)
    print("[OK] no observation used 'appliedTotal' as a stat_name")


if __name__ == "__main__":
    main()

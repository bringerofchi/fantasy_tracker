"""
Survey (read-only diagnostic): for every player in ESPN's full 2025
population, attempt parse_weekly_projection() for week 5 directly,
catching failures instead of letting the first one propagate — the
opposite of fetch()'s fail-fast behavior.

Why this exists: run_full_population_test.py proved fetch() aborts on
the FIRST player lacking a weekly projection ('Steelers D/ST') — but
because it fails fast, that tells us only that at least one such player
exists, not how many, and not whether the gap is confined to D/ST or
also affects individual skill-position players (which would be a much
more serious problem for the adapter's "every player -> a valid
observation" assumption).

This script does NOT change any adapter behavior. It calls
espn_core.parse_weekly_projection() directly per player, purely to
characterize the true scope of the gap with real evidence before
deciding how — or whether — to change the adapter's contract. Same
"prove it, don't guess" rule as the rest of this project.

Run from this folder, with real internet access:

    python survey_full_population_projection_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "survey_full_population_projection_coverage_output.txt"
_log_file = open(_LOG_PATH, "w", encoding="utf-8")


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self._streams:
            s.flush()


sys.stdout = _Tee(sys.__stdout__, _log_file)
sys.stderr = _Tee(sys.__stderr__, _log_file)


def _pause_before_exit(code: int) -> None:
    try:
        input("\nPress Enter to close this window...")
    except EOFError:
        pass
    sys.exit(code)


def _crash_handler(exc_type, exc_value, tb):
    import traceback
    print("\n" + "=" * 70)
    print("SCRIPT CRASHED (unhandled exception)")
    print("=" * 70)
    traceback.print_exception(exc_type, exc_value, tb)
    print(f"\nFull output saved to: {_LOG_PATH}")
    _pause_before_exit(2)


sys.excepthook = _crash_handler

print(f"(writing a full copy of this output to {_LOG_PATH})\n")

from espn_adapter import ESPNSourceAdapter
from espn_core import parse_weekly_projection

SEASON_ID = 2025
WEEK_NUMBER = 5

POSITION_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}

adapter = ESPNSourceAdapter()

print("=" * 70)
print(f"PROJECTION COVERAGE SURVEY: season={SEASON_ID} week={WEEK_NUMBER}, full population")
print("=" * 70)

raw = adapter._fetch_raw(SEASON_ID)
players = raw["players"]
print(f"\nPopulation size: {len(players)}\n")

by_position_total: dict = {}
by_position_failed: dict = {}
failures: list = []  # (name, position_id, error_type, message)

for entry in players:
    p = entry.get("player", {})
    name = p.get("fullName", "?")
    pos_id = p.get("defaultPositionId", "?")
    by_position_total[pos_id] = by_position_total.get(pos_id, 0) + 1
    try:
        parse_weekly_projection(entry, SEASON_ID, WEEK_NUMBER)
    except Exception as e:
        by_position_failed[pos_id] = by_position_failed.get(pos_id, 0) + 1
        failures.append((name, pos_id, type(e).__name__, str(e)))

print("Coverage by position:")
for pos_id in sorted(by_position_total, key=lambda x: (isinstance(x, str), x)):
    total = by_position_total[pos_id]
    failed = by_position_failed.get(pos_id, 0)
    label = POSITION_NAMES.get(pos_id, f"id={pos_id}")
    print(f"  {label:6s}: {total - failed}/{total} have a valid weekly projection")

print(f"\nTotal failures: {len(failures)} / {len(players)}")

if failures:
    print("\nFirst 20 failures:")
    for name, pos_id, err_type, msg in failures[:20]:
        label = POSITION_NAMES.get(pos_id, f"id={pos_id}")
        print(f"  [{err_type}] {name!r} ({label}): {msg[:140]}")

    non_dst_failures = [f for f in failures if f[1] != 16]
    if non_dst_failures:
        print(f"\n[IMPORTANT] {len(non_dst_failures)} failures are NOT D/ST — this is not a D/ST-only issue.")
        print("Non-D/ST failures (all of them):")
        for name, pos_id, err_type, msg in non_dst_failures:
            label = POSITION_NAMES.get(pos_id, f"id={pos_id}")
            print(f"    [{err_type}] {name!r} ({label}): {msg[:140]}")
    else:
        print(f"\n[CONFIRMED] all {len(failures)} failures are D/ST (positionId=16) — this appears to be")
        print("a D/ST-specific gap, not a general individual-player problem.")

    # Look at the raw shape of one D/ST failure to understand WHY, not just THAT.
    dst_failures = [f for f in failures if f[1] == 16]
    if dst_failures:
        example_name = dst_failures[0][0]
        example_entry = next(e for e in players if e.get("player", {}).get("fullName") == example_name)
        stats = example_entry.get("player", {}).get("stats", [])
        print(f"\nFull stats[] for example D/ST failure ({example_name!r}), {len(stats)} entries:")
        for s in stats:
            print(
                f"    seasonId={s.get('seasonId')} scoringPeriodId={s.get('scoringPeriodId')} "
                f"statSourceId={s.get('statSourceId')} statSplitTypeId={s.get('statSplitTypeId')} "
                f"appliedTotal={s.get('appliedTotal')}"
            )
        has_any_projected = any(s.get("statSourceId") == 1 for s in stats)
        print(f"\nDoes this D/ST have ANY statSourceId=1 (projected) entry, for ANY period? {has_any_projected}")
else:
    print("\n[UNEXPECTED] no failures found in this survey, but fetch() aborted earlier on "
          "'Steelers D/ST' — investigate the discrepancy (e.g. a transient response difference).")

print("\n" + "=" * 70)
print("RESULT: SURVEY COMPLETE (this script does not pass/fail — it's diagnostic only)")
print("=" * 70)
_pause_before_exit(0)

"""
Full-population ESPN fetch test.

Every other live script in this project (qc_phase4c.py, run_espn_ingest.py)
scopes ESPNSourceAdapter.fetch() to a curated 8-player set via player_ids.
This script deliberately does NOT scope it, and instead fetches ESPN's
ENTIRE player population for one (season, week) in a single call.

Why this matters (backend v1 review, 2026-09): parse_weekly_projection()
is never wrapped in a try/except anywhere in this codebase, by design —
the project's rule is "fail loudly, never silently substitute a default."
That's proven correct for a curated set of known-active starters, but it
has never actually been exercised against the full ~1000-player
population, which includes inactive players, practice-squad players, and
anyone else ESPN's endpoint lists but who might legitimately lack a
weekly projection entry for the requested period. If even one such
player exists in the population, this design means the ENTIRE fetch()
call aborts — not just that one player.

This script's job is just to find out, with real evidence, which of
these is true:
  (a) fetch() succeeds across the full population with zero exceptions
      (i.e. every listed player has a valid weekly projection), or
  (b) fetch() aborts, and if so, exactly which player/condition
      triggered it — so that's a real, specific case to decide about,
      not a hypothetical.

Run from this folder, with real internet access:

    python run_full_population_test.py

Writes run_full_population_test_output.txt next to this script, and (if
the fetch succeeds) a real tracker_full_population.db you can inspect.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "run_full_population_test_output.txt"
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

import storage
from espn_adapter import ESPNSourceAdapter
from espn_core import ESPNDataAnomalyError, ESPNSchemaError
from normalized import DataType

SEASON_ID = 2025   # historical, fully played — same week used elsewhere in this project
WEEK_NUMBER = 5

adapter = ESPNSourceAdapter()

print("=" * 70)
print(f"FULL-POPULATION FETCH TEST: season={SEASON_ID} week={WEEK_NUMBER}, no player_ids filter")
print("=" * 70)
print("(fetching ESPN's entire player population in one request; may take a few seconds)\n")

# Peek at raw population size up front, purely for diagnostic context —
# uses the adapter's internal _fetch_raw() rather than the public
# fetch()/parse path, so this works even if the full parse below fails.
try:
    raw = adapter._fetch_raw(SEASON_ID)
    population_size = len(raw.get("players", []))
    print(f"Raw population returned by ESPN: {population_size} players\n")
except Exception as e:
    print(f"[FAIL] could not even fetch the raw population: {type(e).__name__}: {e}")
    _pause_before_exit(2)

try:
    observations = adapter.fetch(SEASON_ID, WEEK_NUMBER)
except ESPNDataAnomalyError as e:
    print("[ANOMALY] fetch() raised ESPNDataAnomalyError — a genuine data-integrity issue,")
    print("NOT an expected absence (this is exactly the case the backend v1 fix was meant to")
    print("surface distinctly instead of silently swallowing):")
    print(f"\n    {e}\n")
    print("Report this player/entry before deciding how to proceed.")
    _pause_before_exit(1)
except ESPNSchemaError as e:
    print("[FAIL] fetch() aborted the ENTIRE batch on ESPNSchemaError (fail-loud design):")
    print(f"\n    {e}\n")
    print(f"This confirms the review's open question: at least one player in the")
    print(f"{population_size}-player population lacks a weekly projection entry, and the")
    print("current design aborts everything rather than skipping just that player.")
    print("This needs a decision about the adapter's contract, not a silent patch.")
    _pause_before_exit(1)
except Exception as e:
    print(f"[FAIL] fetch() raised an unexpected exception type {type(e).__name__}: {e}")
    _pause_before_exit(2)

print(f"[PASS] fetch() succeeded across the full {population_size}-player population with zero exceptions.")
print(f"Total observations: {len(observations)}")

by_type: dict[str, int] = {}
for o in observations:
    by_type[o.data_type.value] = by_type.get(o.data_type.value, 0) + 1
for k, v in sorted(by_type.items()):
    print(f"    {k}: {v}")

projections = [o for o in observations if o.data_type == DataType.PROJECTION]
distinct_players_with_projection = len({o.player.source_player_id for o in projections})
print(f"\nDistinct players with a valid weekly projection: {distinct_players_with_projection} / {population_size}")

if distinct_players_with_projection == population_size:
    print("\n[CONFIRMED] every player in the full population produced a valid PROJECTION observation.")
else:
    print("\n[UNEXPECTED] fetch() succeeded but produced fewer projections than players in the")
    print("population — investigate this; it shouldn't be possible given parse_weekly_projection()")
    print("is never caught.")

# Persist to a real DB too, same pattern as run_espn_ingest.py, to prove
# storage handles this volume cleanly as well (not just the parse step).
DB_PATH = Path(__file__).parent / "tracker_full_population.db"
if DB_PATH.exists():
    DB_PATH.unlink()
conn = storage.connect(DB_PATH)
result = storage.save_observations(conn, observations)
print(f"\nPersisted to {DB_PATH.name}: {result}")
conn.close()

print("\n" + "=" * 70)
print("RESULT: PASSED")
print("=" * 70)
_pause_before_exit(0)

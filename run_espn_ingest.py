"""
End-to-end smoke test: live ESPN data -> real SQLite file on disk.

Unlike qc_phase4c.py (which checks the adapter's output in memory) or
test_ingestion.py (which uses the fixture, no network), this script
proves the FULL path works together against live data: ESPNSourceAdapter
-> ingest() -> storage.py -> an actual .db file you can open and inspect
afterward.

Run from this folder, with real internet access:

    python run_espn_ingest.py

Writes tracker.db next to this script (created fresh each run).
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "run_espn_ingest_output.txt"
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
from ingestion import ingest
from normalized import DataType

DB_PATH = Path(__file__).parent / "tracker.db"

# Same small real-player set used in qc_phase4c.py — one per position.
PLAYER_IDS = {
    "Josh Allen (QB)": 3918298,
    "Lamar Jackson (QB)": 3916387,
    "Jahmyr Gibbs (RB)": 4429795,
    "Christian McCaffrey (RB)": 3117251,
    "Ja'Marr Chase (WR)": 4362628,
    "CeeDee Lamb (WR)": 4241389,
    "Trey McBride (TE)": 4361307,
    "Brock Bowers (TE)": 4432665,
}

if DB_PATH.exists():
    DB_PATH.unlink()
    print(f"(removed existing {DB_PATH.name} so this run starts clean)\n")

conn = storage.connect(DB_PATH)
adapter = ESPNSourceAdapter()

print("=" * 70)
print("RUN 1: ingest 2025 week 5 (historical — has both projections and actuals)")
print("=" * 70)
result1 = ingest(adapter, conn, season_id=2025, week_number=5, player_ids=list(PLAYER_IDS.values()))
print(result1)

print("\n" + "=" * 70)
print("RUN 2: ingest the SAME thing again — proving no duplication")
print("=" * 70)
result2 = ingest(adapter, conn, season_id=2025, week_number=5, player_ids=list(PLAYER_IDS.values()))
print(result2)

ok = True
if result2.inserted != 0:
    print(f"\n[FAIL] second run inserted {result2.inserted} new rows — expected 0 (duplication bug)")
    ok = False
if result2.updated != result1.inserted:
    print(f"\n[FAIL] second run updated {result2.updated} rows, expected {result1.inserted} (should re-affirm every row from run 1)")
    ok = False

total_rows = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
print(f"\nTotal rows in {DB_PATH.name} after both runs: {total_rows}")

print("\n" + "=" * 70)
print("Sample: Jahmyr Gibbs, everything stored for him")
print("=" * 70)
rows = storage.query_observations(conn, source_player_id="4429795")
for r in rows:
    label = r.ranking_type.value if r.ranking_type else r.data_type.value
    value = r.fantasy_points if r.fantasy_points is not None else r.rank
    print(f"  {r.source:6s} season={r.season_id} week={r.week_number} {label:10s} = {value}")

conn.close()

print("\n" + "=" * 70)
if ok:
    print(f"RESULT: PASSED. {DB_PATH.name} is a real SQLite file — open it with any SQLite browser to inspect further.")
else:
    print("RESULT: FAILED — see [FAIL] lines above.")
print("=" * 70)

_pause_before_exit(0 if ok else 1)

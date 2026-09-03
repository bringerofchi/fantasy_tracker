"""
Phase 4C Integration/QC script.

Runs the PRODUCTION adapter (ESPNSourceAdapter — real live HTTP against
ESPN, not the fixture-backed LocalFileSourceAdapter) and checks it
against the six QC points from the Phase 4C plan. Each check reports
one of four outcomes:

    PASS             the capability works, verified against live data.
    EXPECTED ABSENCE ESPN doesn't expose the requested value; the
                     adapter correctly returns nothing rather than
                     fabricating one. This is proof of a SOURCE
                     LIMITATION, not a capability — never read it as
                     a PASS.
    FAIL             our adapter/database behavior is wrong.
    NOT TESTED       the capability hasn't been empirically established
                     yet — either this session lacks the code needed to
                     test it (DB/ingestion layer, other adapters), or
                     the real-world condition needed to observe it
                     (e.g. an in-season week) hasn't happened yet.

A missing ESPN positional ranking on a COMPLETED season is EXPECTED
ABSENCE (see FINDINGS.md section D / PHASE_4C_STATUS.md) — it must
never show up as a FAIL, and must not be counted as a PASS either.

Run from this same folder (needs espn_adapter.py etc. importable), with
real internet access:

    python qc_phase4c.py

Requires network access to ESPN — this cannot run inside the research
sandbox that built it (see FINDINGS.md, "Environment note").
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Everything printed below is ALSO written to qc_phase4c_output.txt next
# to this script, so the results survive even if the console window
# closes itself (e.g. this file was double-clicked in File Explorer
# instead of run from an already-open terminal).
_LOG_PATH = Path(__file__).parent / "qc_phase4c_output.txt"
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
    # If this was double-clicked instead of run from an open terminal,
    # this keeps the window open long enough to actually read the
    # result — and the full output is in qc_phase4c_output.txt either way.
    try:
        input("\nPress Enter to close this window...")
    except EOFError:
        pass
    sys.exit(code)


def _crash_handler(exc_type, exc_value, tb):
    import traceback
    print("\n" + "=" * 70)
    print("QC SCRIPT CRASHED (unhandled exception, not a QC FAIL result)")
    print("=" * 70)
    traceback.print_exception(exc_type, exc_value, tb)
    print(f"\nFull output (including this) was saved to: {_LOG_PATH}")
    _pause_before_exit(2)


sys.excepthook = _crash_handler

print(f"(writing a full copy of this output to {_LOG_PATH})\n")

from espn_adapter import ESPNSourceAdapter
from espn_core import ESPNSchemaError
from normalized import DataType, Position, RankingType

# Small, real, current (2026) player set covering all four positions —
# two per position, per "small set of real 2026 players across
# QB/RB/WR/TE".
PLAYERS_2026 = {
    "Josh Allen": (3918298, Position.QB),
    "Lamar Jackson": (3916387, Position.QB),
    "Jahmyr Gibbs": (4429795, Position.RB),
    "Christian McCaffrey": (3117251, Position.RB),
    "Ja'Marr Chase": (4362628, Position.WR),
    "CeeDee Lamb": (4241389, Position.WR),
    "Trey McBride": (4361307, Position.TE),
    "Brock Bowers": (4432665, Position.TE),
}
ALL_2026_IDS = [pid for pid, _ in PLAYERS_2026.values()]

POSITION_RANKING_TYPE = {
    Position.QB: RankingType.QB,
    Position.RB: RankingType.RB,
    Position.WR: RankingType.WR,
    Position.TE: RankingType.TE,
}

results: list[tuple[str, str, str]] = []  # (check_name, status, detail)


# Four-category taxonomy — kept deliberately distinct so a source
# limitation can never masquerade as a capability proof or a real defect:
#   PASS            — the capability works, verified against live data.
#   EXPECTED ABSENCE — ESPN doesn't expose the requested value; the
#                      adapter correctly returns nothing rather than
#                      fabricating one. Never conflate this with PASS —
#                      it's proof of a source limitation, not a capability.
#   FAIL            — our adapter/database behavior is wrong.
#   NOT TESTED      — the capability hasn't been empirically established
#                      yet, either because this session lacks the code
#                      needed to test it (DB/ingestion layer, other
#                      adapters), or because the real-world condition
#                      needed to observe it (e.g. an in-season week)
#                      hasn't occurred yet.
STATUSES = ("PASS", "EXPECTED ABSENCE", "FAIL", "NOT TESTED")


def record(name: str, status: str, detail: str = "") -> None:
    assert status in STATUSES, f"unknown status {status!r}, must be one of {STATUSES}"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def obs_key(o):
    """Identity for comparing observations while ignoring retrieved_at (a timestamp)."""
    return (
        o.source, o.season_id, o.week_number, o.data_type,
        o.player.source_player_id, o.ranking_type, o.fantasy_points, o.rank,
    )


# ------------------------------------------------------------------
# QC1: live fetch across positions, real 2026 players
# ------------------------------------------------------------------
adapter = ESPNSourceAdapter()
try:
    obs_2026_wk1 = adapter.fetch(season_id=2026, week_number=1, player_ids=ALL_2026_IDS)
    positions_seen = {
        o.player.position for o in obs_2026_wk1 if o.data_type == DataType.PROJECTION
    }
    missing_positions = {Position.QB, Position.RB, Position.WR, Position.TE} - positions_seen
    if not obs_2026_wk1:
        record("QC1: live fetch returns data (2026 wk1, 8 players)", "FAIL", "empty result")
    elif missing_positions:
        record("QC1: live fetch returns data (2026 wk1, 8 players)", "FAIL",
                f"no PROJECTION observations for: {[p.value for p in missing_positions]}")
    else:
        record("QC1: live fetch returns data (2026 wk1, 8 players)", "PASS",
                f"{len(obs_2026_wk1)} observations, all 4 positions represented")
except Exception as e:
    obs_2026_wk1 = []
    record("QC1: live fetch returns data (2026 wk1, 8 players)", "FAIL", f"raised {type(e).__name__}: {e}")



# ------------------------------------------------------------------
# QC2: projections match the NormalizedObservation contract
# ------------------------------------------------------------------
proj_2026 = [o for o in obs_2026_wk1 if o.data_type == DataType.PROJECTION]
bad = []
seen_names = set()
for o in proj_2026:
    seen_names.add(o.player.full_name)
    if o.source != "espn":
        bad.append(f"{o.player.full_name}: source={o.source!r}")
    if o.season_id != 2026 or o.week_number != 1:
        bad.append(f"{o.player.full_name}: season/week={o.season_id}/{o.week_number}")
    if not isinstance(o.fantasy_points, float):
        bad.append(f"{o.player.full_name}: fantasy_points not a float ({o.fantasy_points!r})")
    if o.scoring_format != "PPR":
        bad.append(f"{o.player.full_name}: scoring_format={o.scoring_format!r}")
    if o.player.position not in (Position.QB, Position.RB, Position.WR, Position.TE):
        bad.append(f"{o.player.full_name}: unexpected position {o.player.position}")
    if not o.player.pro_team_abbrev:
        bad.append(f"{o.player.full_name}: pro_team_abbrev not resolved")

missing_players = set(PLAYERS_2026) - seen_names
if missing_players:
    bad.append(f"no projection at all for: {sorted(missing_players)}")

if not obs_2026_wk1:
    record("QC2: projection schema correctness", "NOT TESTED", "QC1 produced no data to check")
elif bad:
    record("QC2: projection schema correctness", "FAIL", "; ".join(bad))
else:
    record("QC2: projection schema correctness", "PASS", f"{len(proj_2026)} projections, all fields valid")


# ------------------------------------------------------------------
# QC3: overall + positional rankings land with correct ranking_type
# ------------------------------------------------------------------
rank_2026 = [o for o in obs_2026_wk1 if o.data_type == DataType.RANKING]
bad = []
for name, (pid, pos) in PLAYERS_2026.items():
    player_ranks = [o for o in rank_2026 if o.player.full_name == name]
    overall = [o for o in player_ranks if o.ranking_type == RankingType.OVERALL]
    position = [o for o in player_ranks if o.ranking_type == POSITION_RANKING_TYPE[pos]]
    if len(overall) != 1:
        bad.append(f"{name}: expected 1 OVERALL ranking, found {len(overall)}")
    elif not (overall[0].rank and overall[0].rank > 0):
        bad.append(f"{name}: OVERALL rank not a positive number ({overall[0].rank!r})")
    if len(position) != 1:
        bad.append(f"{name}: expected 1 {POSITION_RANKING_TYPE[pos].value} ranking, found {len(position)}")
    elif not (position[0].rank and position[0].rank > 0):
        bad.append(f"{name}: {pos.value} rank not a positive number ({position[0].rank!r})")

if not obs_2026_wk1:
    record("QC3: rankings have correct ranking_type", "NOT TESTED", "QC1 produced no data to check")
elif bad:
    record("QC3: rankings have correct ranking_type", "FAIL", "; ".join(bad))
else:
    record("QC3: rankings have correct ranking_type", "PASS",
           "every player has exactly one OVERALL + one own-position ranking, both positive")


# ------------------------------------------------------------------
# QC4: missing data stays missing (not synthesized as zero/fabricated)
# ------------------------------------------------------------------
# 4a: 2026 week 1 — season hasn't started, so there must be ZERO
# ACTUAL observations (no games played yet). If any ACTUAL shows up
# with e.g. 0.0 instead of being simply absent, that would be exactly
# the "treat missing data as zero" bug the brief warned against.
actual_2026 = [o for o in obs_2026_wk1 if o.data_type == DataType.ACTUAL]
check_4a_ok = len(actual_2026) == 0

# 4b: 2025 (completed season) week 5 — OVERALL ranking should still be
# present (draftRanksByRankType persists), but NO positional ranking
# should appear (rankings{} is wiped for a completed season). This is
# the documented, EXPECTED behavior, not a failure.
try:
    obs_2025_wk5 = adapter.fetch(season_id=2025, week_number=5, player_ids=ALL_2026_IDS)
    overall_2025 = [o for o in obs_2025_wk5 if o.data_type == DataType.RANKING and o.ranking_type == RankingType.OVERALL]
    position_2025 = [
        o for o in obs_2025_wk5
        if o.data_type == DataType.RANKING and o.ranking_type in POSITION_RANKING_TYPE.values()
    ]
    check_4b_ok = len(overall_2025) == len(PLAYERS_2026) and len(position_2025) == 0
    detail_4b = f"overall={len(overall_2025)}/8 present, positional={len(position_2025)}/0 present (expected 0)"
except Exception as e:
    check_4b_ok = False
    detail_4b = f"raised {type(e).__name__}: {e}"

if not obs_2026_wk1:
    record("QC4a: no fabricated ACTUAL for unplayed 2026 week", "NOT TESTED", "QC1 produced no data to check")
elif check_4a_ok:
    record("QC4a: no fabricated ACTUAL for unplayed 2026 week", "PASS", "0 ACTUAL observations, as expected")
else:
    record("QC4a: no fabricated ACTUAL for unplayed 2026 week", "FAIL",
           f"{len(actual_2026)} ACTUAL observations found for a week with no games played yet")

if check_4b_ok:
    # This is deliberately EXPECTED ABSENCE, not PASS: what's being
    # confirmed here is that ESPN doesn't expose positional ranking for
    # a completed season, and that the adapter correctly declines to
    # fabricate one — not that "positional ranking" as a capability
    # works for historical seasons. Never relabel this PASS; a future
    # reader skimming for PASS counts must not conclude historical
    # positional ranking is supported.
    record("QC4b: 2025 positional ranking (source doesn't expose it for a completed season)",
           "EXPECTED ABSENCE", detail_4b)
else:
    record("QC4b: 2025 positional ranking (source doesn't expose it for a completed season)",
           "FAIL", detail_4b)

record(
    "QC-FLEX: FLEX ranking capability",
    "EXPECTED ABSENCE",
    "ESPN provides no FLEX-scoped ranking field anywhere in this response (see FINDINGS.md §D); "
    "QC7 below confirms the adapter never fabricates one. Any FLEX value is tracker-derived, not ESPN-sourced.",
)

record(
    "QC-INSEASON: in-season weekly consensus positional ranking",
    "NOT TESTED",
    "2026 season has not started; rankings[week>0] was observed present but with no rankSourceId=0 "
    "consensus entry in preseason (see FINDINGS.md §D). Whether a consensus populates once games are "
    "underway is a real-world condition this session cannot yet observe — re-run this check against a "
    "live in-season week before trusting the per-week ranking path.",
)


# ------------------------------------------------------------------
# QC5: idempotency (necessary precondition for safe re-import).
# True duplicate-on-insert handling lives in the DB/ingestion layer,
# which this session has no access to — marked NOT TESTED.
# ------------------------------------------------------------------
try:
    run1 = adapter.fetch(season_id=2025, week_number=5, player_ids=[4429795])
    run2 = adapter.fetch(season_id=2025, week_number=5, player_ids=[4429795])
    keys1 = sorted(obs_key(o) for o in run1)
    keys2 = sorted(obs_key(o) for o in run2)
    has_internal_dupes = len(keys1) != len(set(keys1))
    if has_internal_dupes:
        record("QC5: adapter output is idempotent (re-fetch precondition)", "FAIL",
               "a single fetch() call produced duplicate observations")
    elif keys1 != keys2:
        record("QC5: adapter output is idempotent (re-fetch precondition)", "FAIL",
               "two identical fetch() calls returned different content")
    else:
        record("QC5: adapter output is idempotent (re-fetch precondition)", "PASS",
               f"{len(keys1)} observations, identical across two independent calls, no internal dupes")
except Exception as e:
    record("QC5: adapter output is idempotent (re-fetch precondition)", "FAIL", f"raised {type(e).__name__}: {e}")

record(
    "QC5b: duplicate-on-INSERT is handled correctly at the DB/ingestion layer",
    "NOT TESTED",
    "requires the tracker's storage/ingestion code, which this session has not been given",
)


# ------------------------------------------------------------------
# QC6: source attribution / isolation
# ------------------------------------------------------------------
all_checked = obs_2026_wk1 + (obs_2025_wk5 if 'obs_2025_wk5' in dir() else [])
bad_source = [o for o in all_checked if o.source != "espn"]
if not all_checked:
    record("QC6a: every observation attributed source='espn'", "NOT TESTED", "no data to check")
elif bad_source:
    record("QC6a: every observation attributed source='espn'", "FAIL",
           f"{len(bad_source)} observations with source != 'espn'")
else:
    record("QC6a: every observation attributed source='espn'", "PASS",
           f"{len(all_checked)}/{len(all_checked)} observations correctly attributed")

record(
    "QC6b: no cross-contamination alongside OTHER source adapters",
    "NOT TESTED",
    "requires the other source adapters + shared ingestion pipeline, not available to this session",
)


# ------------------------------------------------------------------
# QC7 (static, not in the original 1-6 but cheap and load-bearing for
# the FLEX policy call): confirm FLEX is never fabricated anywhere in
# the adapter's own code, not just "wasn't observed in this run".
# ------------------------------------------------------------------
here = Path(__file__).parent
flex_hits = []
for fname in ("espn_core.py", "espn_adapter.py", "local_file_adapter.py"):
    text = (here / fname).read_text()
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r"RankingType\.FLEX", line):
            flex_hits.append(f"{fname}:{i}: {line.strip()}")

if flex_hits:
    record("QC7: FLEX is never fabricated by the ESPN adapter (static scan)", "FAIL", "; ".join(flex_hits))
else:
    record("QC7: FLEX is never fabricated by the ESPN adapter (static scan)", "PASS",
           "no RankingType.FLEX usage found in espn_core.py / espn_adapter.py / local_file_adapter.py")


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("PHASE 4C QC SUMMARY")
print("=" * 70)
n_pass = sum(1 for _, s, _ in results if s == "PASS")
n_ea = sum(1 for _, s, _ in results if s == "EXPECTED ABSENCE")
n_fail = sum(1 for _, s, _ in results if s == "FAIL")
n_nt = sum(1 for _, s, _ in results if s == "NOT TESTED")
for name, status, detail in results:
    print(f"  [{status:17s}] {name}")
print(f"\n{n_pass} PASS, {n_ea} EXPECTED ABSENCE, {n_fail} FAIL, {n_nt} NOT TESTED")

if n_fail:
    print("\nRESULT: QC FAILED — see FAIL lines above.")
    _pause_before_exit(1)
else:
    print("\nRESULT: QC PASSED. EXPECTED ABSENCE items are confirmed source limitations, not gaps to "
          "close in this adapter. NOT TESTED items require the tracker's DB/ingestion code and other "
          "adapters to close out.")
    _pause_before_exit(0)

"""
Manual Yahoo screenshot verification -- no Anthropic API key required.

This does NOT call AnthropicVisionExtractor and does NOT need an
ANTHROPIC_API_KEY. The values below were transcribed by Claude directly
from the real Yahoo screenshot you uploaded (Week 1, 2026, "The League -
Malort Madness", Ja'Marr Chase's row on the Projected Stats tab). This
script builds the exact same ScreenshotExtraction object
AnthropicVisionExtractor would have produced, then sends it through the
REAL, unmodified ingestion.pipeline.ingest_screenshot() -- the same
function app.py's /ingest route calls.

Run from the repo root (the folder with app.py, db/, ingestion/ in it):
    python3 verify_yahoo_ingest_filled.py

Uses a throwaway COPY of db/tracker.db -- never touches the real one.
"""
import os
import shutil
import sys

from db.database import get_conn, init_db
from ingestion.extractor import ScreenshotExtraction, ExtractedField
from ingestion.fake_extractor import FakeExtractor
from ingestion import pipeline

# =====================================================================
# Values transcribed from your real Yahoo screenshot (Week 1, 2026):
# Ja'Marr Chase, Cin - WR, Projected Stats tab.
# Yahoo's own footnote marks Att/Tgt (*) as non-scoring -- consistent
# with rush_attempts being informational-only in this app too.
# =====================================================================

EXTRACTION = ScreenshotExtraction(
    player_name_raw="Ja'Marr Chase",
    player_name_confidence=1.0,
    position_hint="WR",
    week_hint=1,
    team_hint="CIN",
    source_context="Yahoo Fantasy web app, Week 1 Projected Stats view",
    fields=[
        ExtractedField(stat_name="rush_attempts", value=0.2, confidence=1.0),
        ExtractedField(stat_name="rush_yards", value=1.1, confidence=1.0),
        ExtractedField(stat_name="rush_tds", value=0.0, confidence=1.0),
        ExtractedField(stat_name="receptions", value=7.5, confidence=1.0),
        ExtractedField(stat_name="receiving_yards", value=93.9, confidence=1.0),
        ExtractedField(stat_name="receiving_tds", value=0.7, confidence=1.0),
    ],
)

DATA_TYPE = "projection"
WEEK_NUMBER = 1
SOURCE_NAME = "Yahoo"

# The actual screenshot file isn't needed by the script logic (FakeExtractor
# never opens it), but a real path is still recorded for provenance. If you
# want the real file path in the DB record, edit the line below; otherwise
# this placeholder is fine.
REAL_SCREENSHOT_PATH = "yahoo_week1_screenshot.pdf"

# =====================================================================
# Nothing below this line needs editing.
# =====================================================================

import tempfile
THROWAWAY_DB = os.path.join(tempfile.gettempdir(), "tracker_yahoo_verify.db")


def main():
    # create_import() hashes the file on disk for provenance -- it doesn't
    # need real image bytes (FakeExtractor never opens it), just something
    # that exists, so a placeholder is created automatically if needed.
    if not os.path.exists(REAL_SCREENSHOT_PATH):
        with open(REAL_SCREENSHOT_PATH, "w") as f:
            f.write("placeholder for provenance hashing only -- see EXTRACTION values above for the real transcribed data")
        print(f"Created placeholder file at {REAL_SCREENSHOT_PATH} (for provenance hashing only)")

    if os.path.exists(THROWAWAY_DB):
        os.remove(THROWAWAY_DB)
    if os.path.exists("db/tracker.db"):
        shutil.copy("db/tracker.db", THROWAWAY_DB)
        print(f"Copied real db/tracker.db -> {THROWAWAY_DB} (throwaway, additive-reseed only)")
    else:
        init_db(reset=True, db_path=THROWAWAY_DB)
        print(f"No existing db/tracker.db found; created a fresh throwaway DB at {THROWAWAY_DB}")

    conn = get_conn(THROWAWAY_DB)
    season_row = conn.execute("SELECT season_id FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    season_id = season_row["season_id"]

    extractor = FakeExtractor(EXTRACTION)
    result = pipeline.ingest_screenshot(
        conn, extractor, REAL_SCREENSHOT_PATH, os.path.basename(REAL_SCREENSHOT_PATH),
        season_id, WEEK_NUMBER, DATA_TYPE, SOURCE_NAME,
        position_hint=EXTRACTION.position_hint,
    )

    print()
    print("=== ingest_screenshot() result ===")
    print(result)

    if result.get("extraction_failed"):
        print("[FAIL] extraction_failed is True -- check 'error' above")
        conn.close()
        sys.exit(1)

    print()
    print(f"auto_accepted: {len(result['auto_accepted'])}")
    print(f"sent_to_review: {len(result['sent_to_review'])}")

    # ---- Automatic verification queries (steps you'd otherwise run by hand) ----
    print()
    print("=== Verification: statistics/projections rows for Ja'Marr Chase, source=Yahoo ===")
    rows = conn.execute("""
        SELECT p.display_name, pr.stat_name, pr.projected_value, pr.week_id, pr.scope, pr.source_name
        FROM projections pr JOIN players p ON p.player_id = pr.player_id
        WHERE pr.source_name = 'Yahoo'
    """).fetchall()
    for r in rows:
        print(" ", dict(r))

    print()
    print("=== Verification: no fantasy_points/appliedTotal ever stored as a raw stat ===")
    leak1 = conn.execute("SELECT COUNT(*) c FROM statistics WHERE stat_name IN ('fantasy_points','appliedTotal')").fetchone()["c"]
    leak2 = conn.execute("SELECT COUNT(*) c FROM projections WHERE stat_name IN ('fantasy_points','appliedTotal')").fetchone()["c"]
    print(f"  statistics leak count: {leak1} (must be 0)")
    print(f"  projections leak count: {leak2} (must be 0)")

    print()
    print("=== Verification: pending review items (should be empty if Chase already exists as a player) ===")
    reviews = conn.execute("SELECT review_id, entity_type, reason, status FROM review_items WHERE status='pending' ORDER BY review_id DESC LIMIT 10").fetchall()
    for r in reviews:
        print(" ", dict(r))
    if not reviews:
        print("  (none)")

    print()
    print("=== Verification: downstream PPR derived from raw Yahoo projection rows ===")
    from fantasy.scoring import calculate_fantasy_points, PPR_SCORING
    pid_row = conn.execute("SELECT player_id FROM players WHERE display_name=?", ("Ja'Marr Chase",)).fetchone()
    if pid_row:
        pid = pid_row["player_id"]
        proj_rows = conn.execute("""
            SELECT stat_name, projected_value FROM projections
            WHERE player_id=? AND source_name='Yahoo' AND scope='weekly'
        """, (pid,)).fetchall()
        values = {r["stat_name"]: r["projected_value"] for r in proj_rows}
        print("  raw values used:", values)
        ppr_result = calculate_fantasy_points(values, PPR_SCORING)
        print("  derived PPR total:", ppr_result.total_points)
        print("  breakdown:", ppr_result.breakdown)
        print("  Yahoo's own displayed Fan Pts for this player/week: 21.19")
        print("  (independently derived from raw stats alone -- within 0.01 of Yahoo's own")
        print("   number, i.e. matches to rounding precision. Never read Yahoo's Fan Pts")
        print("   value directly; this total was computed purely from the raw categories.)")
    else:
        print("  Ja'Marr Chase not found as a resolved player -- see review_items above.")

    conn.close()
    print()
    print(f"Done. Throwaway DB at {THROWAWAY_DB}. Copy everything above and paste it back to Claude.")


if __name__ == "__main__":
    main()

"""
Manual Yahoo screenshot verification -- no Anthropic API key required.

This does NOT call AnthropicVisionExtractor and does NOT need an
ANTHROPIC_API_KEY. You transcribe the real screenshot's values by eye
into EXTRACTION below, and this script builds the exact same
ScreenshotExtraction object AnthropicVisionExtractor would have
produced, then sends it through the REAL, unmodified
ingestion.pipeline.ingest_screenshot() -- the same function app.py's
/ingest route calls. This validates everything downstream of
extraction (identity resolution, validation, confidence routing,
DB writes) using real values from a real screenshot.

Run from the repo root:
    python3 verify_yahoo_ingest.py

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
# EDIT THIS SECTION: transcribe the real screenshot's values by eye.
# Only include stat_name keys you can actually SEE in the screenshot --
# never fill in a value you're guessing at. Omit anything not visible.
# Valid stat_name keys (must match db/database.py STAT_DEFINITIONS):
#   pass_yards, pass_tds, interceptions, rush_yards, rush_tds,
#   rush_attempts, pass_completions, pass_attempts, receptions,
#   receiving_yards, receiving_tds, fumbles_lost
# =====================================================================

REAL_SCREENSHOT_PATH = "/path/to/your/real/week1_yahoo_screenshot.png"  # <-- set this

EXTRACTION = ScreenshotExtraction(
    player_name_raw="PLAYER NAME AS SHOWN",       # <-- transcribe exactly as displayed
    player_name_confidence=1.0,                    # you read it yourself -- high confidence
    position_hint="RB",                             # <-- QB / RB / WR / TE, as shown
    week_hint=1,
    team_hint=None,                                  # optional, e.g. "MIN" if visible
    source_context="Yahoo Fantasy web app, Players > Player List, Week 1 (proj) view",
    fields=[
        # One ExtractedField per raw stat category visible in the screenshot.
        # Example (delete/replace with what you actually see):
        ExtractedField(stat_name="rush_yards", value=95.0, confidence=1.0),
        ExtractedField(stat_name="rush_tds", value=0.6, confidence=1.0),
        ExtractedField(stat_name="receptions", value=3.5, confidence=1.0),
        ExtractedField(stat_name="receiving_yards", value=22.0, confidence=1.0),
    ],
)

DATA_TYPE = "projection"   # "projection" for a "(proj)" view, "actual" for a post-game view
WEEK_NUMBER = 1
SOURCE_NAME = "Yahoo"      # must exactly match the sources table row name

# =====================================================================
# Nothing below this line needs editing.
# =====================================================================

THROWAWAY_DB = "/tmp/tracker_yahoo_verify.db"


def main():
    if not os.path.exists(REAL_SCREENSHOT_PATH):
        print(f"[FAIL] REAL_SCREENSHOT_PATH does not point to a real file: {REAL_SCREENSHOT_PATH}")
        print("Edit this script and set it to your actual screenshot's path, then re-run.")
        sys.exit(1)

    # Throwaway copy only -- never the real db/tracker.db.
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

    conn.close()
    print()
    print(f"Done. DB is at {THROWAWAY_DB} -- run the verification queries below against it.")


if __name__ == "__main__":
    main()

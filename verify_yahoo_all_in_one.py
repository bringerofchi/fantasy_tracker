"""
All-in-one Yahoo verification: fixes the rush_attempts registry gap
directly (via SQL, no manual file editing needed) and re-runs the full
Yahoo screenshot -> pipeline -> DB verification in one shot.

Run from the repo root:
    python verify_yahoo_all_in_one.py
"""
import os
import shutil
import sys
import tempfile

from db.database import get_conn
from ingestion.extractor import ScreenshotExtraction, ExtractedField
from ingestion.fake_extractor import FakeExtractor
from ingestion import pipeline

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
REAL_SCREENSHOT_PATH = "yahoo_week1_screenshot.pdf"
THROWAWAY_DB = os.path.join(tempfile.gettempdir(), "tracker_yahoo_verify2.db")


def main():
    if not os.path.exists(REAL_SCREENSHOT_PATH):
        with open(REAL_SCREENSHOT_PATH, "w") as f:
            f.write("placeholder for provenance hashing only")

    if os.path.exists(THROWAWAY_DB):
        os.remove(THROWAWAY_DB)
    shutil.copy("db/tracker.db", THROWAWAY_DB)
    print(f"Copied real db/tracker.db -> {THROWAWAY_DB} (throwaway only)")

    conn = get_conn(THROWAWAY_DB)

    # --- Fix 1: add the missing rush_attempts catalog rows directly (SQL,
    # additive only -- no existing rows touched, nothing deleted) ---
    added = 0
    for position in ("QB", "RB", "WR", "TE"):
        cur = conn.execute(
            "INSERT OR IGNORE INTO stat_definitions (position, stat_name, display_label) VALUES (?, 'rush_attempts', 'Rush Attempts')",
            (position,),
        )
        added += cur.rowcount
    conn.commit()
    print(f"Added {added} rush_attempts catalog row(s) to the throwaway copy")

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
    print(f"auto_accepted: {len(result['auto_accepted'])}")
    print(f"sent_to_review: {len(result['sent_to_review'])}")

    print()
    print("=== projections rows for Ja'Marr Chase, source=Yahoo ===")
    for r in conn.execute("""
        SELECT p.display_name, pr.stat_name, pr.projected_value, pr.week_id, pr.scope, pr.source_name
        FROM projections pr JOIN players p ON p.player_id = pr.player_id
        WHERE pr.source_name = 'Yahoo'
    """):
        print(" ", dict(r))

    print()
    print("=== fantasy_points/appliedTotal leak check ===")
    leak1 = conn.execute("SELECT COUNT(*) c FROM statistics WHERE stat_name IN ('fantasy_points','appliedTotal')").fetchone()["c"]
    leak2 = conn.execute("SELECT COUNT(*) c FROM projections WHERE stat_name IN ('fantasy_points','appliedTotal')").fetchone()["c"]
    print(f"  statistics: {leak1} (must be 0)  projections: {leak2} (must be 0)")

    print()
    print("=== pending review items ===")
    reviews = conn.execute("SELECT review_id, reason FROM review_items WHERE status='pending' ORDER BY review_id DESC LIMIT 5").fetchall()
    for r in reviews:
        print(" ", dict(r))
    if not reviews:
        print("  (none)")

    print()
    print("=== downstream PPR from raw Yahoo rows ===")
    from fantasy.scoring import calculate_fantasy_points, PPR_SCORING
    pid = conn.execute("SELECT player_id FROM players WHERE display_name=?", ("Ja'Marr Chase",)).fetchone()["player_id"]
    proj_rows = conn.execute(
        "SELECT stat_name, projected_value FROM projections WHERE player_id=? AND source_name='Yahoo' AND scope='weekly'",
        (pid,),
    ).fetchall()
    values = {r["stat_name"]: r["projected_value"] for r in proj_rows}
    print("  raw values:", values)
    ppr = calculate_fantasy_points(values, PPR_SCORING)
    print("  derived PPR total:", ppr.total_points, " (Yahoo displayed: 21.19)")

    conn.close()
    print()
    print("Done. Paste all of this output back to Claude.")


if __name__ == "__main__":
    main()

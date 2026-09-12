"""
Standalone import script for ESPN's "Complete 2026 Projections" page, saved
as PDF (File > Print > Save as PDF from fantasy.espn.com/football/players/projections).
Safe to re-run: unchanged values are idempotent-per-source, changed values
version and preserve history, same as any other re-collection.

Usage:
    python3 scripts/import_espn_season_projections.py /path/to/file.pdf [week_number]
    python3 scripts/import_espn_season_projections.py /path/to/directory_of_pdfs [week_number]

Every player not already in your roster will have their observations routed
to the Review Queue rather than silently skipped or auto-created — check
/review after running this to resolve them if you want that data trusted.

Requires pdftotext (part of poppler-utils) to be installed.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_conn
from ingestion.espn_projections_adapter import ESPNSeasonProjectionsAdapter
from ingestion import pipeline


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 import_espn_season_projections.py /path/to/file_or_directory.pdf [week_number]")
        sys.exit(1)

    file_path = sys.argv[1]
    week_number = int(sys.argv[2]) if len(sys.argv) > 2 else 1  # irrelevant for scope='season' data, kept for interface compatibility

    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    adapter = ESPNSeasonProjectionsAdapter("ESPN", file_path)

    result = pipeline.ingest_collector_batch(conn, adapter, season["season_id"], week_number=week_number)
    conn.close()

    if result.get("fetch_failed"):
        print(f"Import failed: {result.get('error')}")
        sys.exit(1)

    print(f"Auto-accepted: {len(result['auto_accepted'])}")
    print(f"Sent to review: {len(result['sent_to_review'])}")
    print(f"Duplicates skipped: {len(result['duplicates_skipped'])}")
    print(f"Errors (malformed rows, skipped): {len(result['errors'])}")
    if result["sent_to_review"]:
        print("\nMost of 'sent to review' is expected on a first run — it's every "
              "player in the file who isn't in your roster yet. Check /review to resolve them.")


if __name__ == "__main__":
    main()

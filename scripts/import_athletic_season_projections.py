"""
Standalone import script for The Athletic's season-long projections/rankings
xlsx export (AthleticSeasonXlsxAdapter). Run this again whenever you get an
updated copy of the file — re-running is safe: unchanged values are
idempotent no-ops, changed values create a new version and (for rankings)
flag a conflict for review, exactly like any other re-collection.

Usage:
    python3 scripts/import_athletic_season_projections.py /path/to/the/file.xlsx [week_number]

Every player not already in your roster (see /players) will have their
observations routed to the Review Queue rather than silently skipped or
auto-created — check /review after running this to resolve them (accept,
correct, or create the player) if you want that data trusted.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_conn
from ingestion.athletic_xlsx_adapter import AthleticSeasonXlsxAdapter
from ingestion import pipeline


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 import_athletic_season_projections.py /path/to/file.xlsx [week_number]")
        sys.exit(1)

    file_path = sys.argv[1]
    week_number = int(sys.argv[2]) if len(sys.argv) > 2 else 1  # irrelevant for scope='season' data, kept for interface compatibility

    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    adapter = AthleticSeasonXlsxAdapter("The Athletic", file_path)

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
              "player in the file who isn't in your roster yet. Check /review to "
              "resolve them.")


if __name__ == "__main__":
    main()

"""
Bulk-resolves every pending 'player_not_found' review item by creating the
player and then explicitly accepting each of that player's pending
observations against it — an authorized action, not a guess: this is only
ever run when the person has explicitly said "track every player in these
projections" (or equivalent), not invoked automatically by any import.

Only ever touches reason='player_not_found' items with a known position.
Ambiguous-identity items are deliberately left untouched — "which of these
two existing players did you mean" isn't something a blanket "create
everyone" instruction resolves, and this script doesn't try to guess that.

Usage:
    python3 scripts/bulk_create_players_from_review_queue.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_conn
from db import repository as repo
from ingestion import pipeline


def main():
    conn = get_conn()

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND ri.reason='player_not_found'
           AND po.position IS NOT NULL AND po.position != ''"""
    ).fetchall()

    if not pending:
        print("No pending 'player_not_found' items to resolve.")
        return

    # Distinct (name, position) pairs -> create one player each
    distinct_pairs = sorted({(r["extracted_player_name"], r["position"]) for r in pending})
    name_pos_to_player_id = {}
    created = 0
    for name, position in distinct_pairs:
        existing_id, match_type = repo.resolve_player(conn, name, position)
        if match_type == "exact":
            name_pos_to_player_id[(name, position)] = existing_id
        elif match_type == "none":
            new_id = repo.create_player(conn, name, position)
            name_pos_to_player_id[(name, position)] = new_id
            created += 1
        else:
            # 'alias' or 'ambiguous' — don't touch; a human already resolved
            # this some other way, or it genuinely needs a human decision.
            print(f"Skipping '{name}' ({position}) — identity match was '{match_type}', not a clean 'none'.")

    print(f"Created {created} new players ({len(distinct_pairs) - created} already existed).")

    accepted, failed = 0, 0
    for row in pending:
        key = (row["extracted_player_name"], row["position"])
        player_id = name_pos_to_player_id.get(key)
        if player_id is None:
            continue  # skipped above (ambiguous/alias case)
        try:
            pipeline.accept_proposed_observation(conn, row["proposed_id"], player_id_override=player_id)
            accepted += 1
        except ValueError as e:
            failed += 1
            print(f"Could not accept proposed_id={row['proposed_id']} ({row['extracted_player_name']}): {e}")

    print(f"Accepted {accepted} observations, {failed} failed.")

    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    total_players = conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    print(f"Remaining pending review items: {remaining}")
    print(f"Total players now in roster: {total_players}")
    conn.close()


if __name__ == "__main__":
    main()

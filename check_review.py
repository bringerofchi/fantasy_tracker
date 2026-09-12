import sqlite3, json, os, tempfile

db_path = os.path.join(tempfile.gettempdir(), "tracker_yahoo_verify.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT details_json FROM review_items WHERE review_id=3775").fetchone()
print(json.dumps(json.loads(row["details_json"]), indent=2))
conn.close()
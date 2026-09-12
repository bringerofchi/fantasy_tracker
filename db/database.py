"""
Database connection helper + initialization/seeding.
"""
import sqlite3
import os
import re
import hashlib

DB_PATH = os.environ.get("NFL_TRACKER_DB_PATH") or os.path.join(os.path.dirname(__file__), "tracker.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_conn(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/suffixes for identity matching (sec. 8)."""
    n = name.lower().strip()
    n = re.sub(r"[.\'\-]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def file_hash(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


NFL_TEAMS = [
    ("Arizona Cardinals", "ARI"), ("Atlanta Falcons", "ATL"), ("Baltimore Ravens", "BAL"),
    ("Buffalo Bills", "BUF"), ("Carolina Panthers", "CAR"), ("Chicago Bears", "CHI"),
    ("Cincinnati Bengals", "CIN"), ("Cleveland Browns", "CLE"), ("Dallas Cowboys", "DAL"),
    ("Denver Broncos", "DEN"), ("Detroit Lions", "DET"), ("Green Bay Packers", "GB"),
    ("Houston Texans", "HOU"), ("Indianapolis Colts", "IND"), ("Jacksonville Jaguars", "JAX"),
    ("Kansas City Chiefs", "KC"), ("Las Vegas Raiders", "LV"), ("Los Angeles Chargers", "LAC"),
    ("Los Angeles Rams", "LAR"), ("Miami Dolphins", "MIA"), ("Minnesota Vikings", "MIN"),
    ("New England Patriots", "NE"), ("New Orleans Saints", "NO"), ("New York Giants", "NYG"),
    ("New York Jets", "NYJ"), ("Philadelphia Eagles", "PHI"), ("Pittsburgh Steelers", "PIT"),
    ("San Francisco 49ers", "SF"), ("Seattle Seahawks", "SEA"), ("Tampa Bay Buccaneers", "TB"),
    ("Tennessee Titans", "TEN"), ("Washington Commanders", "WAS"),
]

SOURCES = [
    ("NFL", "stat_provider", "yes"),
    ("Yahoo", "ranking_provider", "no"),
    ("ESPN", "ranking_provider", "no"),
    ("The Athletic", "ranking_provider", "no"),
    ("User", "user", "no"),
    ("AI Extraction", "ai_extraction", "no"),
]

# Decision 1 answers: user-selected stat fields per position
STAT_DEFINITIONS = {
    "QB": ["pass_yards", "pass_tds", "interceptions", "rush_yards", "rush_tds", "rush_attempts",
           "pass_completions", "pass_attempts"],
    "RB": ["rush_yards", "rush_tds", "rush_attempts", "receptions", "receiving_yards", "receiving_tds",
           "fumbles_lost"],
    "WR": ["receptions", "receiving_yards", "receiving_tds", "rush_yards", "rush_tds", "rush_attempts",
           "fumbles_lost"],
    "TE": ["receptions", "receiving_yards", "receiving_tds", "rush_attempts", "fumbles_lost"],
}

STAT_LABELS = {
    "pass_yards": "Pass Yards", "pass_tds": "Pass TDs", "interceptions": "INTs",
    "rush_yards": "Rush Yards", "rush_tds": "Rush TDs", "rush_attempts": "Rush Attempts",
    "pass_completions": "Completions", "pass_attempts": "Attempts",
    "receptions": "Receptions", "receiving_yards": "Receiving Yards",
    "receiving_tds": "Receiving TDs", "fumbles_lost": "Fumbles Lost",
}


def init_db(reset=False, db_path=None):
    path = db_path or DB_PATH
    if reset and os.path.exists(path):
        os.remove(path)
    conn = get_conn(path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())

    # Seed teams
    for name, abbr in NFL_TEAMS:
        conn.execute(
            "INSERT OR IGNORE INTO teams (name, abbreviation) VALUES (?, ?)", (name, abbr)
        )

    # Seed sources
    for name, stype, canon in SOURCES:
        conn.execute(
            "INSERT OR IGNORE INTO sources (name, source_type, is_canonical_actuals) VALUES (?, ?, ?)",
            (name, stype, canon),
        )

    # Seed stat definitions (Decision 1)
    for position, stats in STAT_DEFINITIONS.items():
        for stat_name in stats:
            conn.execute(
                "INSERT OR IGNORE INTO stat_definitions (position, stat_name, display_label) VALUES (?, ?, ?)",
                (position, stat_name, STAT_LABELS[stat_name]),
            )

    # Seed 2026 season (Decision 3: 2026 only) + 18 regular season weeks
    conn.execute("INSERT OR IGNORE INTO seasons (year, status) VALUES (2026, 'active')")
    season_id = conn.execute("SELECT season_id FROM seasons WHERE year = 2026").fetchone()["season_id"]
    for wk in range(1, 19):
        conn.execute(
            "INSERT OR IGNORE INTO weeks (season_id, week_number, week_type, status) VALUES (?, ?, 'regular', 'upcoming')",
            (season_id, wk),
        )
    conn.commit()
    conn.close()
    return season_id


if __name__ == "__main__":
    sid = init_db(reset=True)
    print(f"Database initialized at {DB_PATH}, season_id={sid}")

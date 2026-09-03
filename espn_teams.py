"""
ESPN proTeamId -> NFL team abbreviation mapping.

CONFIDENCE NOTE (read before trusting this table):
Only 4 of these 32 entries were independently verified this session by
cross-referencing a known real player against their known real team via
a live ESPN API response (see FINDINGS.md, section F/identity):

    proTeamId 2  -> BUF  (confirmed via Josh Allen)
    proTeamId 4  -> CIN  (confirmed via Ja'Marr Chase)
    proTeamId 8  -> DET  (confirmed via Jahmyr Gibbs)
    proTeamId 22 -> ARI  (confirmed via Trey McBride)

The remaining 28 entries are the widely-published, long-stable ESPN
proTeamId convention (unchanged across many public reverse-engineering
write-ups over several years), reproduced here for completeness. They
were NOT independently re-verified against live 2026 data this session.
Treat pro_team_abbrev as medium-confidence; source_player.pro_team_id
(the raw ESPN id) is always populated directly from the live response
and is the ground truth if this table is ever wrong.
"""

PRO_TEAM_ID_TO_ABBREV: dict[int, str] = {
    0: "FA",
    1: "ATL",
    2: "BUF",
    3: "CHI",
    4: "CIN",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GB",
    10: "TEN",
    11: "IND",
    12: "KC",
    13: "LV",
    14: "LAR",
    15: "MIA",
    16: "MIN",
    17: "NE",
    18: "NO",
    19: "NYG",
    20: "NYJ",
    21: "PHI",
    22: "ARI",
    23: "PIT",
    24: "LAC",
    25: "SF",
    26: "SEA",
    27: "TB",
    28: "WSH",
    29: "CAR",
    30: "JAX",
    33: "BAL",
    34: "HOU",
}

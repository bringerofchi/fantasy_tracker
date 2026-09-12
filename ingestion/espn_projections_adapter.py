"""
Phase 4C real adapter #2: ESPN's "Complete 2026 Projections" page, saved as
PDF (File > Print > Save as PDF from fantasy.espn.com/football/players/projections).

Same acquisition-only boundary as AthleticSeasonXlsxAdapter: this file reads
the saved PDF and produces NormalizedObservations. No trust or persistence
logic lives here — every observation still goes through the unmodified
shared pipeline.

Scope, decided deliberately rather than defaulted:
- Only "2026 PROJECTIONS" is imported (data_type='projection', scope='season').
  ESPN's "2025 STATISTICS" column is last season's historical box score —
  useful as human context, but not what "compare 2026 projections vs 2026
  actuals" needs. Real 2026 actuals come from the NFL canonical source at
  weekly granularity, already correctly modeled. Importing ESPN's 2025 column
  would require extending the `statistics` table with the same scope/nullable-
  week_id machinery projections/rankings already got, for a table this app
  doesn't need populated from this source. Deliberately not done.
- D/ST and K rows are skipped entirely — this app only tracks QB/RB/WR/TE
  (an early Phase 1 scope decision), and D/ST and K have entirely different
  stat schemas anyway.
- No ranking data on this page at all (it's a projections table, not a
  ranking list) — this adapter produces projections only.

Known, real limitation of parsing a *printed* PDF export (not a clean data
file): ESPN's page layout gets cut off at the print margin, which
consistently drops the LAST stat column for every position — QB rushing TDs,
RB receiving TDs, WR/TE rushing TDs are simply never present in this specific
export format. This isn't guessed around or backfilled; those observations
are just never produced, which is exactly how "missing" is supposed to work
here — no different in kind from a value ESPN itself never published.
"""
import re
import subprocess
from typing import List

from ingestion.collector import SourceAdapter, NormalizedObservation, AdapterUnavailable

_SPLIT_COL = 56  # empirically confirmed against real extracted text: this is where
                 # the "YEAR / 2025 STATISTICS / 2026 PROJECTIONS / 2026 OUTLOOK"
                 # column consistently starts, regardless of what (if anything)
                 # occupies the player-name/team/position column on the same line.

_POSITIONS = {"QB", "RB", "WR", "TE"}
_STATUS_WORDS = {"Questionable", "Injured Reserve", "Out", "Suspended", "Doubtful"}

# Position-aware column index -> our stat_name. Index positions come from
# ESPN's own header row (e.g. QB: C/A, YDS, TD, INT, CAR, YDS, [TD, truncated]).
# None means "ESPN reports this but we don't track it" (CAR/attempts counts,
# AVG, targets) — skipped, not guessed into something we do track.
_QB_COLUMNS = [None, "pass_yards", "pass_tds", "interceptions", None, "rush_yards", "rush_tds"]
_RB_COLUMNS = [None, "rush_yards", None, "rush_tds", "receptions", "receiving_yards", "receiving_tds"]
_WR_TE_COLUMNS = [None, "receptions", "receiving_yards", None, "receiving_tds", None, "rush_yards", "rush_tds"]

_PROJECTION_CONFIDENCE = 0.85  # right at the auto-accept threshold: a real value parsed
                                # from PDF-derived text is less certain than a clean
                                # spreadsheet cell (Athletic adapter uses 0.9) — text-
                                # position parsing has more ways to be subtly wrong.


class ESPNSeasonProjectionsAdapter(SourceAdapter):
    """file_path may be a single PDF or a directory of PDFs (ESPN's listing is
    paginated across up to 21 saved pages; passing a directory processes all
    of them in one fetch() call)."""

    def __init__(self, source_name: str, file_path: str):
        self.source_name = source_name
        self.file_path = file_path

    def fetch(self, season_id: int, week_number: int) -> List[NormalizedObservation]:
        import os
        if os.path.isdir(self.file_path):
            pdf_paths = sorted(
                os.path.join(self.file_path, f) for f in os.listdir(self.file_path) if f.lower().endswith(".pdf")
            )
            if not pdf_paths:
                raise AdapterUnavailable(f"No PDF files found in directory: {self.file_path}", transient=True)
        elif os.path.isfile(self.file_path):
            pdf_paths = [self.file_path]
        else:
            raise AdapterUnavailable(f"Source path not found: {self.file_path}", transient=True)

        observations: List[NormalizedObservation] = []
        for path in pdf_paths:
            text = self._extract_text(path)
            observations.extend(self._parse_text(text))
        return observations

    def _extract_text(self, pdf_path: str) -> str:
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", pdf_path, "-"],
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError as e:
            raise AdapterUnavailable("pdftotext is not installed (part of poppler-utils)", transient=False) from e
        except subprocess.TimeoutExpired as e:
            raise AdapterUnavailable(f"pdftotext timed out on {pdf_path}", transient=True) from e
        if result.returncode != 0:
            raise AdapterUnavailable(f"pdftotext failed on {pdf_path}: {result.stderr}", transient=False)
        return result.stdout

    def _split_cols(self, line: str):
        return line[:_SPLIT_COL].strip(), line[_SPLIT_COL:].strip()

    def _parse_text(self, text: str) -> List[NormalizedObservation]:
        lines = text.split("\n")

        # Group into blocks, each anchored by a "YEAR ..." header line (the
        # right-column header row that starts every player's record).
        blocks = []
        current_header, current = None, []
        for line in lines:
            left, right = self._split_cols(line)
            if right.startswith("YEAR"):
                if current_header is not None:
                    blocks.append((current_header, current))
                current_header, current = right, []
            else:
                current.append((left, right))
        if current_header is not None:
            blocks.append((current_header, current))

        observations = []
        for header, block in blocks:
            observations.extend(self._parse_block(header, block))
        return observations

    def _parse_block(self, header: str, block) -> List[NormalizedObservation]:
        header_tokens = header.split()[1:]  # drop leading "YEAR"

        name, team_pos, proj_tokens = None, None, None
        for left, right in block:
            if left and left not in _STATUS_WORDS and self._ends_with_position(left):
                team_pos = left
            elif left and left not in _STATUS_WORDS and name is None:
                name = left
            if right.startswith("2026 PROJECTIONS"):
                proj_tokens = right[len("2026 PROJECTIONS"):].split()

        if not name or not team_pos or proj_tokens is None:
            return []  # couldn't confidently locate all three — skip, don't guess

        position = team_pos.split()[-1]
        if position not in _POSITIONS:
            return []  # D/ST, K, or unrecognized shape — out of scope for this app

        column_map = (_QB_COLUMNS if position == "QB" else
                      _RB_COLUMNS if position == "RB" else
                      _WR_TE_COLUMNS)  # WR and TE share a column shape

        observations = []
        for idx, (header_tok, value_tok) in enumerate(zip(header_tokens, proj_tokens)):
            if header_tok == "C/A":
                observations.extend(self._parse_completions_attempts(name, position, value_tok))
                continue
            if idx >= len(column_map) or column_map[idx] is None:
                continue
            if value_tok == "--":
                continue  # missing, not zero — no observation emitted at all
            try:
                value = float(value_tok)
            except ValueError:
                continue  # unparseable token — skip rather than guess
            observations.append(NormalizedObservation(
                player_name_raw=name, position_hint=position, data_type="projection",
                stat_name=column_map[idx], value=value, confidence=_PROJECTION_CONFIDENCE, scope="season",
            ))
        return observations

    def _parse_completions_attempts(self, name: str, position: str, token: str) -> List[NormalizedObservation]:
        if "/" not in token:
            return []
        comp, att = token.split("/", 1)
        observations = []
        for stat_name, raw in (("pass_completions", comp), ("pass_attempts", att)):
            if raw == "--":
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            observations.append(NormalizedObservation(
                player_name_raw=name, position_hint=position, data_type="projection",
                stat_name=stat_name, value=value, confidence=_PROJECTION_CONFIDENCE, scope="season",
            ))
        return observations

    @staticmethod
    def _ends_with_position(text: str) -> bool:
        tokens = text.split()
        return bool(tokens) and tokens[-1] in _POSITIONS

"""
Phase 4A: source adapter framework.

    Scheduler (not built yet — Phase 4B) -> Collector -> Raw Observation -> Phase 3 pipeline

A SourceAdapter's only job is fetch(): turn whatever a source provides into a
list of NormalizedObservation. Everything after that — identity resolution,
validation, confidence routing, auto-accept/review, versioning, conflict
detection — is the SAME Phase 3 pipeline screenshots use (see
ingestion.pipeline.ingest_collector_batch, which reuses the identical
_route_field() that ingest_screenshot() uses). No source-specific logic is
allowed to leak past fetch().

Network note: this sandbox's egress allowlist does not include any sports
data provider (only api.anthropic.com, package registries, and a few infra
domains), so a live HTTP scraper cannot actually be exercised here. The one
adapter implemented for Phase 4A, LocalFileSourceAdapter, reads a normalized
JSON payload from disk instead of a network call — this is a real, honest
collector (weekly data exports/CSV-JSON dumps are a legitimate collection
mechanism many tools use), and it proves out the full contract end-to-end.
An HTTP-based adapter (e.g. requests.get(...) instead of open(...)) would
implement the exact same SourceAdapter interface and requires no pipeline
changes — the transport is deliberately not part of the contract.
"""
import json
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class NormalizedObservation:
    """The one normalized shape every collector must produce, regardless of
    source. This is the boundary: adapter-specific parsing/scraping happens
    entirely inside fetch(); nothing past this dataclass knows or cares where
    the data came from.

    Field order note: week_number moved to the optional group (default None)
    for the Phase 4C season-scope extension — a season-long/preseason
    observation genuinely has no week. Python dataclasses require every field
    after the first defaulted one to also have a default, which is why
    week_number now sits alongside team_hint/ranking_type/scope rather than
    up with the other required fields. Existing callers construct this with
    keyword arguments, so this reordering doesn't break anything positionally."""
    player_name_raw: str
    position_hint: Optional[str]
    data_type: str          # 'actual', 'projection', or 'ranking' (Phase 4C prerequisite)
    stat_name: str           # for data_type='ranking' this is ignored by the pipeline;
                              # pass 'ranking' by convention. ranking_type below is the
                              # real structured field for rankings.
    value: Optional[float]
    confidence: float        # 0.0-1.0. Structured sources are typically high-confidence,
                              # but this stays explicit rather than assumed, so a source
                              # that is itself uncertain about a field can say so.
    week_number: Optional[int] = None    # None only valid when scope='season'
    team_hint: Optional[str] = None
    ranking_type: Optional[str] = None   # overall/QB/RB/WR/TE/FLEX — only meaningful when data_type='ranking'
    scope: str = "weekly"                # 'weekly' (week_number required) or 'season' (week_number None) —
                                           # Phase 4C season-scope extension, same principle as ranking_type:
                                           # a real structured field, never inferred from week_number being None.


class AdapterUnavailable(Exception):
    """Raised when a source can't be reached/parsed this run (source outage,
    malformed page, auth failure, etc). The pipeline treats this the same way
    it treats ExtractorUnavailable: fail the run cleanly, don't write partial
    garbage, don't crash the caller.

    `transient` distinguishes "worth retrying soon" (network blip, temporary
    auth failure) from "will not fix itself by retrying" (the source file/page
    is itself malformed — retrying immediately just reproduces the same
    failure). Phase 4B's retry policy reads this to decide backoff behavior.
    Defaults to True (the safer assumption when an adapter doesn't say)."""
    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


class SourceAdapter:
    """Interface every collector implements. `source_name` must match an
    existing row in the `sources` table (that's how the ingestion pipeline
    knows which source_id owns the collection_runs entry, and what to write
    into statistics.source_id / projections.source_name)."""
    source_name: str

    def fetch(self, season_id: int, week_number: int) -> List[NormalizedObservation]:
        raise NotImplementedError


class LocalFileSourceAdapter(SourceAdapter):
    """
    Reads a normalized JSON payload from disk:
        [{"player_name": "...", "position": "WR", "week_number": 1,
          "data_type": "actual", "stat_name": "receiving_yards", "value": 95,
          "confidence": 0.98, "team": "MIN"},
         {"player_name": "...", "position": "WR", "week_number": 1,
          "data_type": "ranking", "ranking_type": "overall", "value": 5,
          "confidence": 0.95},
         {"player_name": "...", "position": "QB", "data_type": "projection",
          "stat_name": "pass_yards", "value": 4200, "confidence": 0.9,
          "scope": "season"}]

    Entries with "scope": "season" have no week_number at all and are
    returned on every fetch() call regardless of the requested week — a
    season-long observation isn't filtered by week. This is safe even if
    fetch() is called once per week in some batch cycle, since add_projection/
    add_ranking are idempotent on an unchanged value. Entries without an
    explicit "scope" key default to "weekly" and are filtered by week_number
    exactly as before.

    This is the one adapter implemented completely for Phase 4A/4C-prerequisite.
    A future HTTP-based adapter differs only in how fetch() obtains this same
    JSON shape (a network call instead of a file read) — the pipeline
    downstream is identical either way.
    """
    def __init__(self, source_name: str, file_path: str):
        self.source_name = source_name
        self.file_path = file_path

    def fetch(self, season_id: int, week_number: int) -> List[NormalizedObservation]:
        try:
            with open(self.file_path) as f:
                raw = json.load(f)
        except FileNotFoundError as e:
            # Could be a timing issue (scheduled run fired before the export landed) —
            # worth retrying on the normal backoff schedule.
            raise AdapterUnavailable(f"Source file not found: {self.file_path}", transient=True) from e
        except json.JSONDecodeError as e:
            # The file exists but its content is broken — an immediate retry will see
            # the exact same broken content. Not transient.
            raise AdapterUnavailable(f"Source file is not valid JSON: {self.file_path} ({e})", transient=False) from e

        observations = []
        for entry in raw:
            entry_scope = entry.get("scope", "weekly")
            if entry_scope == "weekly" and entry.get("week_number") != week_number:
                continue  # this adapter's file may contain multiple weeks; only fetch the requested one
            observations.append(NormalizedObservation(
                player_name_raw=entry.get("player_name", ""),
                position_hint=entry.get("position"),
                week_number=(week_number if entry_scope == "weekly" else None),
                data_type=entry.get("data_type", "actual"),
                stat_name=entry.get("stat_name", "ranking" if entry.get("data_type") == "ranking" else ""),
                value=entry.get("value"),
                confidence=float(entry.get("confidence", 1.0)),
                team_hint=entry.get("team"),
                ranking_type=entry.get("ranking_type"),
                scope=entry_scope,
            ))
        return observations

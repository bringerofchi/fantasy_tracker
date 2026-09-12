"""
Phase 4B: scheduler.

    Scheduler.run_due_jobs() -> for each due job -> ingest_collector_batch() -> Phase 3/4A pipeline

This is intentionally timer-independent: Scheduler has no sleep loop, no
thread, no dependency on cron/APScheduler/anything. Something external
(a cron entry calling a small script, a background thread, a manual button
in the UI, a test) decides *when* to call run_due_jobs(). The scheduler's
only job is: given "now", figure out which jobs are due, run them safely
(respecting the DB-level run lock), and update each job's own bookkeeping
(next run time, consecutive failures, needs_attention) based on the outcome.

Nothing here writes to statistics/projections directly, and nothing here
re-implements identity resolution, validation, or conflict handling — all
of that stays exactly as ingest_collector_batch/_route_field already do it.
The scheduler's entire job is orchestration: deciding whether and when to
call that function again.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from ingestion.collector import LocalFileSourceAdapter, SourceAdapter
from ingestion.athletic_xlsx_adapter import AthleticSeasonXlsxAdapter
from ingestion.espn_projections_adapter import ESPNSeasonProjectionsAdapter
from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter
from ingestion import pipeline as ingest_pipeline

# Registry of constructible adapter types, keyed by the string stored in
# scheduled_jobs.adapter_type. Adding a new adapter (Phase 4C) means adding
# one entry here — the scheduler itself needs no changes.
ADAPTER_REGISTRY = {
    "LocalFileSourceAdapter": LocalFileSourceAdapter,
    "AthleticSeasonXlsxAdapter": AthleticSeasonXlsxAdapter,
    "ESPNSeasonProjectionsAdapter": ESPNSeasonProjectionsAdapter,
    "ESPNWeeklyLiveAdapter": ESPNWeeklyLiveAdapter,
}

SQLITE_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str(now: Optional[str] = None) -> str:
    """Returns a timestamp string in the same format SQLite's datetime('now')
    produces, so string comparisons in SQL (next_run_at <= now) stay valid.
    Accepts an explicit `now` for deterministic testing."""
    if now is not None:
        return now
    return datetime.now(timezone.utc).strftime(SQLITE_DATETIME_FMT)


def _add_minutes(ts: str, minutes: int) -> str:
    dt = datetime.strptime(ts, SQLITE_DATETIME_FMT)
    return (dt + timedelta(minutes=minutes)).strftime(SQLITE_DATETIME_FMT)


class Scheduler:
    def __init__(self, conn):
        self.conn = conn

    # ---------------------------------------------------------------
    # Job management
    # ---------------------------------------------------------------

    def create_job(self, source_name: str, adapter_type: str, adapter_config: dict,
                    season_id: int, week_number: int, interval_minutes: int = 60,
                    max_retries: int = 3, retry_backoff_minutes: int = 15, enabled: bool = True) -> int:
        if adapter_type not in ADAPTER_REGISTRY:
            raise ValueError(f"Unknown adapter_type: {adapter_type}. Known: {list(ADAPTER_REGISTRY)}")
        cur = self.conn.execute(
            """INSERT INTO scheduled_jobs
               (source_name, adapter_type, adapter_config_json, season_id, week_number,
                interval_minutes, max_retries, retry_backoff_minutes, enabled, next_run_at)
               VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))""",
            (source_name, adapter_type, json.dumps(adapter_config), season_id, week_number,
             interval_minutes, max_retries, retry_backoff_minutes, int(enabled)),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_enabled(self, job_id: int, enabled: bool):
        self.conn.execute(
            "UPDATE scheduled_jobs SET enabled=?, updated_at=datetime('now') WHERE job_id=?",
            (int(enabled), job_id),
        )
        self.conn.commit()

    def get_job(self, job_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------
    # Due-job selection and execution
    # ---------------------------------------------------------------

    def due_jobs(self, now: Optional[str] = None) -> list:
        now_val = _now_str(now)
        rows = self.conn.execute(
            "SELECT * FROM scheduled_jobs WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at <= ?) "
            "ORDER BY next_run_at",
            (now_val,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _build_adapter(self, job: dict) -> SourceAdapter:
        adapter_cls = ADAPTER_REGISTRY.get(job["adapter_type"])
        if adapter_cls is None:
            raise ValueError(f"Unknown adapter_type: {job['adapter_type']}")
        config = json.loads(job["adapter_config_json"])
        return adapter_cls(job["source_name"], **config)

    def run_job(self, job: dict, now: Optional[str] = None) -> dict:
        """
        Runs one job now (ignores due-ness — due_jobs() is what decides that;
        this always executes, so it can also be used for an explicit manual
        "run now" trigger). Returns {job_id, outcome, ...}. `outcome` is one
        of: 'locked', 'config_error', 'success', 'transient_failure',
        'permanent_failure'.
        """
        now_val = _now_str(now)
        job_id = job["job_id"]

        try:
            adapter = self._build_adapter(job)
        except Exception as e:
            self._apply_config_error(job, str(e), now_val)
            return {"job_id": job_id, "outcome": "config_error", "error": str(e)}

        result = ingest_pipeline.ingest_collector_batch(self.conn, adapter, job["season_id"], job["week_number"])

        if result.get("run_locked"):
            # Another run for this exact (source, season, week) is already active.
            # Don't touch retry bookkeeping — this isn't a failure, just a skip.
            return {"job_id": job_id, "outcome": "locked"}

        if result.get("config_error"):
            self._apply_config_error(job, result.get("error", "unknown config error"), now_val)
            return {"job_id": job_id, "outcome": "config_error", "error": result.get("error")}

        run_row = self.conn.execute(
            "SELECT * FROM collection_runs WHERE run_id=?", (result["collection_run_id"],)
        ).fetchone()
        outcome = self._classify(run_row)
        self._apply_outcome(job, run_row, outcome, now_val)
        return {"job_id": job_id, "outcome": outcome, "collection_run_id": result["collection_run_id"], "result": result}

    def run_due_jobs(self, now: Optional[str] = None) -> list:
        now_val = _now_str(now)
        return [self.run_job(job, now=now_val) for job in self.due_jobs(now_val)]

    # ---------------------------------------------------------------
    # Outcome classification and job bookkeeping
    # ---------------------------------------------------------------

    def _classify(self, run_row) -> str:
        if run_row["status"] in ("successful", "partial"):
            return "success"
        if run_row["status"] == "failed" and run_row["failure_type"] == "transient":
            return "transient_failure"
        # Covers: status='failed' with failure_type='permanent' or NULL (including
        # a batch where every observation was malformed — that's a data problem,
        # not a network blip, so it gets the same non-short-backoff treatment),
        # and status='error' (unexpected exception during processing).
        return "permanent_failure"

    def _apply_outcome(self, job: dict, run_row, outcome: str, now_val: str):
        job_id = job["job_id"]
        if outcome == "success":
            self.conn.execute(
                "UPDATE scheduled_jobs SET consecutive_failures=0, needs_attention=0, last_run_id=?, "
                "last_run_status=?, last_error=NULL, next_run_at=?, updated_at=datetime('now') WHERE job_id=?",
                (run_row["run_id"], run_row["status"], _add_minutes(now_val, job["interval_minutes"]), job_id),
            )
        else:
            consecutive = job["consecutive_failures"] + 1
            error_text = run_row["notes"]
            if outcome == "transient_failure" and consecutive <= job["max_retries"]:
                # Short backoff — worth trying again soon, this looks like a blip.
                next_run_at = _add_minutes(now_val, job["retry_backoff_minutes"])
                needs_attention = 0
            else:
                # Either a permanent/data failure (retrying immediately is pointless),
                # or a transient failure that has now exhausted its retry budget.
                # Fall back to the job's normal cadence rather than hammering the
                # source, and surface it for a human to look at.
                next_run_at = _add_minutes(now_val, job["interval_minutes"])
                needs_attention = 1
            self.conn.execute(
                "UPDATE scheduled_jobs SET consecutive_failures=?, needs_attention=?, last_run_id=?, "
                "last_run_status=?, last_error=?, next_run_at=?, updated_at=datetime('now') WHERE job_id=?",
                (consecutive, needs_attention, run_row["run_id"], run_row["status"], error_text, next_run_at, job_id),
            )
        self.conn.commit()

    def _apply_config_error(self, job: dict, error: str, now_val: str):
        """No collection_runs row exists for a config error (bad adapter_type or
        unknown source — caught before any run could be created), so this updates
        scheduled_jobs directly instead of going through _apply_outcome."""
        job_id = job["job_id"]
        consecutive = job["consecutive_failures"] + 1
        self.conn.execute(
            "UPDATE scheduled_jobs SET consecutive_failures=?, needs_attention=1, last_run_status='config_error', "
            "last_error=?, next_run_at=?, updated_at=datetime('now') WHERE job_id=?",
            (consecutive, error, _add_minutes(now_val, job["interval_minutes"]), job_id),
        )
        self.conn.commit()

    # ---------------------------------------------------------------
    # Stale-run recovery
    # ---------------------------------------------------------------

    def recover_stale_runs(self, stale_minutes: int = 60) -> list:
        """Uses the existing find_stale_collection_runs() from the 4A audit.
        Marking a stale run's status away from 'started' is also what frees
        the run-lock (idx_unique_active_collection_run only blocks while
        status='started'), so this is what actually unblocks that job's next
        scheduled attempt after a process was killed mid-run."""
        stale = ingest_pipeline.find_stale_collection_runs(self.conn, stale_minutes)
        recovered = []
        for run in stale:
            self.conn.execute(
                "UPDATE collection_runs SET status='stale', completed_at=datetime('now'), notes=? WHERE run_id=?",
                ("Auto-recovered: exceeded staleness threshold, presumed crashed process", run["run_id"]),
            )
            recovered.append(run["run_id"])
        if recovered:
            self.conn.commit()
        return recovered

    # ---------------------------------------------------------------
    # Status / visibility
    # ---------------------------------------------------------------

    def job_status(self, job_id: int) -> Optional[dict]:
        job = self.get_job(job_id)
        if not job:
            return None
        last_run = None
        if job["last_run_id"]:
            row = self.conn.execute("SELECT * FROM collection_runs WHERE run_id=?", (job["last_run_id"],)).fetchone()
            last_run = dict(row) if row else None
        active_run = self.conn.execute(
            "SELECT * FROM collection_runs WHERE source_id=(SELECT source_id FROM sources WHERE name=?) "
            "AND season_id=? AND status='started'",
            (job["source_name"], job["season_id"]),
        ).fetchone()
        return {
            "job_id": job["job_id"],
            "source_name": job["source_name"],
            "season_id": job["season_id"],
            "week_number": job["week_number"],
            "enabled": bool(job["enabled"]),
            "currently_running": active_run is not None,
            "next_run_at": job["next_run_at"],
            "consecutive_failures": job["consecutive_failures"],
            "needs_attention": bool(job["needs_attention"]),
            "last_run_status": job["last_run_status"],
            "last_error": job["last_error"],
            "last_run": last_run,
        }

    def list_status(self) -> list:
        job_ids = [r["job_id"] for r in self.conn.execute("SELECT job_id FROM scheduled_jobs ORDER BY job_id").fetchall()]
        return [self.job_status(jid) for jid in job_ids]

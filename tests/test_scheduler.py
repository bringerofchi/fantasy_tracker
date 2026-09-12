import unittest
import tempfile
import os
import json
import threading
from datetime import datetime, timedelta
from db.database import init_db, get_conn
from db import repository as repo
from ingestion.scheduler import Scheduler, _now_str, SQLITE_DATETIME_FMT
from ingestion import pipeline


def write_source_file(path, entries):
    with open(path, "w") as f:
        json.dump(entries, f)


class SchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        self.src_path = tempfile.mktemp(suffix=".json")
        self.scheduler = Scheduler(self.conn)

    def tearDown(self):
        self.conn.close()
        for p in (self.db_path, self.src_path):
            if os.path.exists(p):
                os.remove(p)


class TestJobCreationAndDueSelection(SchedulerTestBase):
    def test_new_job_is_immediately_due(self):
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1)
        due = self.scheduler.due_jobs()
        self.assertEqual([j["job_id"] for j in due], [job_id])

    def test_disabled_job_is_never_due(self):
        self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                   self.season_id, 1, enabled=False)
        self.assertEqual(self.scheduler.due_jobs(), [])

    def test_job_with_future_next_run_at_is_not_due(self):
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1)
        future = _now_str()
        future_dt = datetime.strptime(future, SQLITE_DATETIME_FMT) + timedelta(hours=1)
        self.conn.execute("UPDATE scheduled_jobs SET next_run_at=? WHERE job_id=?",
                           (future_dt.strftime(SQLITE_DATETIME_FMT), job_id))
        self.conn.commit()
        self.assertEqual(self.scheduler.due_jobs(), [])

    def test_unknown_adapter_type_rejected_at_creation(self):
        with self.assertRaises(ValueError):
            self.scheduler.create_job("NFL", "NotARealAdapter", {}, self.season_id, 1)


class TestSuccessfulRunAndReschedule(SchedulerTestBase):
    def test_successful_run_reschedules_at_interval_and_resets_failures(self):
        repo.create_player(self.conn, "Sched WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Sched WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1, interval_minutes=30)
        job = self.scheduler.get_job(job_id)
        now = "2026-01-01 12:00:00"
        outcome = self.scheduler.run_job(job, now=now)
        self.assertEqual(outcome["outcome"], "success")

        updated = self.scheduler.get_job(job_id)
        self.assertEqual(updated["consecutive_failures"], 0)
        self.assertEqual(updated["needs_attention"], 0)
        self.assertEqual(updated["next_run_at"], "2026-01-01 12:30:00")
        self.assertIsNotNone(updated["last_run_id"])

    def test_status_shows_last_run_details(self):
        repo.create_player(self.conn, "Status WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Status WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1)
        self.scheduler.run_job(self.scheduler.get_job(job_id))
        status = self.scheduler.job_status(job_id)
        self.assertFalse(status["currently_running"])
        self.assertEqual(status["last_run_status"], "successful")
        self.assertFalse(status["needs_attention"])


class TestRetryPolicy(SchedulerTestBase):
    def test_transient_failure_gets_short_backoff_retry(self):
        # source file doesn't exist -> FileNotFoundError -> transient
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter",
                                            {"file_path": "/nonexistent/path.json"},
                                            self.season_id, 1, interval_minutes=60, retry_backoff_minutes=5,
                                            max_retries=3)
        job = self.scheduler.get_job(job_id)
        now = "2026-01-01 12:00:00"
        outcome = self.scheduler.run_job(job, now=now)
        self.assertEqual(outcome["outcome"], "transient_failure")

        updated = self.scheduler.get_job(job_id)
        self.assertEqual(updated["consecutive_failures"], 1)
        self.assertEqual(updated["needs_attention"], 0)  # still within retry budget
        self.assertEqual(updated["next_run_at"], "2026-01-01 12:05:00")  # short backoff, not full interval

    def test_transient_failure_exhausting_retries_falls_back_to_normal_interval(self):
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter",
                                            {"file_path": "/nonexistent/path.json"},
                                            self.season_id, 1, interval_minutes=60, retry_backoff_minutes=5,
                                            max_retries=2)
        now = "2026-01-01 12:00:00"
        for _ in range(3):  # exceed max_retries=2
            job = self.scheduler.get_job(job_id)
            outcome = self.scheduler.run_job(job, now=now)

        updated = self.scheduler.get_job(job_id)
        self.assertEqual(updated["consecutive_failures"], 3)
        self.assertTrue(updated["needs_attention"])
        self.assertEqual(outcome["outcome"], "transient_failure")
        # once exhausted, falls back to the full interval rather than the short backoff
        self.assertEqual(updated["next_run_at"], "2026-01-01 13:00:00")

    def test_permanent_failure_never_gets_short_backoff(self):
        with open(self.src_path, "w") as f:
            f.write("{not valid json[[[")  # malformed data, not transient
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1, interval_minutes=60, retry_backoff_minutes=5,
                                            max_retries=3)
        job = self.scheduler.get_job(job_id)
        now = "2026-01-01 12:00:00"
        outcome = self.scheduler.run_job(job, now=now)
        self.assertEqual(outcome["outcome"], "permanent_failure")

        updated = self.scheduler.get_job(job_id)
        self.assertTrue(updated["needs_attention"])  # flagged immediately, no retry budget spent chasing bad data
        self.assertEqual(updated["next_run_at"], "2026-01-01 13:00:00")  # full interval, not short backoff

    def test_config_error_flagged_and_rescheduled_at_interval(self):
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1, interval_minutes=60)
        # corrupt the job's config after creation to simulate a bad adapter_type sneaking in
        self.conn.execute("UPDATE scheduled_jobs SET adapter_type='NoSuchAdapter' WHERE job_id=?", (job_id,))
        self.conn.commit()
        job = self.scheduler.get_job(job_id)
        outcome = self.scheduler.run_job(job, now="2026-01-01 12:00:00")
        self.assertEqual(outcome["outcome"], "config_error")
        updated = self.scheduler.get_job(job_id)
        self.assertTrue(updated["needs_attention"])
        self.assertEqual(updated["last_run_status"], "config_error")

    def test_recovering_from_failure_resets_needs_attention_on_next_success(self):
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1, interval_minutes=60, retry_backoff_minutes=5,
                                            max_retries=1)
        # first: fail (file doesn't exist yet)
        job = self.scheduler.get_job(job_id)
        self.scheduler.run_job(job, now="2026-01-01 12:00:00")
        # now the source comes back online
        repo.create_player(self.conn, "Recovered WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Recovered WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        job = self.scheduler.get_job(job_id)
        outcome = self.scheduler.run_job(job, now="2026-01-01 12:05:00")
        self.assertEqual(outcome["outcome"], "success")
        updated = self.scheduler.get_job(job_id)
        self.assertEqual(updated["consecutive_failures"], 0)
        self.assertFalse(updated["needs_attention"])


class TestRunLockingConcurrency(SchedulerTestBase):
    """Proves run-locking works under real concurrent execution, not just
    sequential calls — two threads racing to run the exact same job."""

    def test_two_concurrent_schedulers_only_one_actually_runs(self):
        """
        Real-thread timing under the GIL is too unreliable to prove this for
        such a short critical section (the same lesson from the 4A audit's
        TOCTOU test) — a fast thread can complete its entire run before the
        other is even scheduled, making a race assertion flaky rather than
        meaningful. Instead, this deterministically constructs the exact
        interleaving: connection A starts a run (holds the DB-level lock),
        and while it's still active, connection B — simulating a second
        scheduler process — attempts the identical job and must be locked out.
        """
        repo.create_player(self.conn, "Concurrent WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Concurrent WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1)

        conn_a = get_conn(self.db_path)
        conn_b = get_conn(self.db_path)

        # Connection A: simulate a scheduler process that has started this exact
        # (source, season, week) run and hasn't finished yet.
        source_id = conn_a.execute("SELECT source_id FROM sources WHERE name='NFL'").fetchone()["source_id"]
        conn_a.execute(
            "INSERT INTO collection_runs (source_id, season_id, week_id, status) VALUES (?, ?, ?, 'started')",
            (source_id, self.season_id, self.week1),
        )
        conn_a.commit()

        # Connection B: a second scheduler process attempts the SAME job while A is still active.
        sched_b = Scheduler(conn_b)
        job = sched_b.get_job(job_id)
        outcome_b = sched_b.run_job(job)
        self.assertEqual(outcome_b["outcome"], "locked")

        # Nothing was written by the locked-out attempt.
        self.assertEqual(conn_b.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"], 0)

        # Once A's run legitimately finishes (status leaves 'started'), the lock
        # is released and a subsequent attempt proceeds normally — the lock isn't
        # permanently stuck, only exclusive while a run is genuinely active.
        conn_a.execute("UPDATE collection_runs SET status='successful', completed_at=datetime('now') WHERE source_id=?",
                        (source_id,))
        conn_a.commit()
        outcome_b2 = sched_b.run_job(sched_b.get_job(job_id))
        self.assertEqual(outcome_b2["outcome"], "success")

        conn_a.close()
        conn_b.close()


class TestStaleRunRecovery(SchedulerTestBase):
    def test_stale_run_is_recovered_and_unblocks_the_lock(self):
        source_id = self.conn.execute("SELECT source_id FROM sources WHERE name='NFL'").fetchone()["source_id"]
        # Simulate a run that was killed mid-flight: status stuck at 'started', old timestamp.
        self.conn.execute(
            "INSERT INTO collection_runs (source_id, season_id, week_id, status, started_at) "
            "VALUES (?, ?, ?, 'started', datetime('now', '-3 hours'))",
            (source_id, self.season_id, self.week1),
        )
        self.conn.commit()

        # Before recovery: the run-lock blocks a new attempt for this exact (source, season, week).
        repo.create_player(self.conn, "Blocked WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Blocked WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        from ingestion.collector import LocalFileSourceAdapter
        adapter = LocalFileSourceAdapter("NFL", self.src_path)
        blocked_result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertTrue(blocked_result.get("run_locked"))

        recovered = self.scheduler.recover_stale_runs(stale_minutes=60)
        self.assertEqual(len(recovered), 1)

        # After recovery: the lock is freed, a new run can proceed normally.
        unblocked_result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
        self.assertFalse(unblocked_result.get("run_locked"))
        self.assertEqual(len(unblocked_result["auto_accepted"]), 1)

    def test_recent_run_not_touched_by_recovery(self):
        source_id = self.conn.execute("SELECT source_id FROM sources WHERE name='NFL'").fetchone()["source_id"]
        self.conn.execute(
            "INSERT INTO collection_runs (source_id, season_id, week_id, status) VALUES (?, ?, ?, 'started')",
            (source_id, self.season_id, self.week1),
        )
        self.conn.commit()
        recovered = self.scheduler.recover_stale_runs(stale_minutes=60)
        self.assertEqual(recovered, [])
        run = self.conn.execute("SELECT status FROM collection_runs").fetchone()
        self.assertEqual(run["status"], "started")  # untouched — genuinely still running


class TestSafeRerunIdempotency(SchedulerTestBase):
    """An interrupted-then-retried run must not create duplicate trusted
    observations or duplicate review items — building on the 4A audit's
    idempotency guarantees, exercised through the scheduler's own retry path."""

    def test_retried_job_after_transient_failure_does_not_duplicate_on_eventual_success(self):
        job_id = self.scheduler.create_job("NFL", "LocalFileSourceAdapter", {"file_path": self.src_path},
                                            self.season_id, 1, retry_backoff_minutes=5, max_retries=3)
        # First attempt: source not there yet -> transient failure, no data written
        job = self.scheduler.get_job(job_id)
        self.scheduler.run_job(job, now="2026-01-01 12:00:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"], 0)

        # Source appears; retry succeeds
        repo.create_player(self.conn, "Retry WR", "WR")
        write_source_file(self.src_path, [
            {"player_name": "Retry WR", "position": "WR", "week_number": 1, "data_type": "actual",
             "stat_name": "receiving_yards", "value": 80, "confidence": 0.99},
        ])
        job = self.scheduler.get_job(job_id)
        outcome = self.scheduler.run_job(job, now="2026-01-01 12:05:00")
        self.assertEqual(outcome["outcome"], "success")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM statistics").fetchone()["c"], 1)

        # A subsequent normal re-run with identical data stays idempotent (no new version)
        job = self.scheduler.get_job(job_id)
        self.scheduler.run_job(job, now="2026-01-01 13:05:00")
        rows = self.conn.execute("SELECT * FROM statistics WHERE stat_name='receiving_yards'").fetchall()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()

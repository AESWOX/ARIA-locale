"""E8: несрочные cron-job'ы в пик откладываются в off-peak (до 6 часов), ручной запуск — сразу."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pathlib import Path

from aria.db import repository as repo
from aria.db.base import session_scope
from aria.scheduler import jobs
from tests.test_wave1_runner_and_policy import _RecordingRunner

MONDAY = (2026, 11, 2)
SATURDAY = (2026, 11, 7)


def _dt(day, hour, minute=0):
    return datetime(*day, hour, minute, tzinfo=timezone.utc)


def _job(now, *, urgent: bool = False, name: str = "night-task") -> str:
    with session_scope() as db:
        row = repo.create_scheduler_job(
            db, name=name, schedule="*/5 * * * *", objective="night work", role="general", urgent=urgent
        )
        repo.update_scheduler_job(db, row.job_id, last_run_at=now - timedelta(minutes=10))
        return str(row.job_id)


def _job_state(job_id: str):
    import uuid

    with session_scope() as db:
        row = repo.get_scheduler_job(db, uuid.UUID(job_id))
        return row.last_run_status, row.defer_until


@pytest.mark.anyio
async def test_peak_defers_to_next_offpeak():
    now = _dt(MONDAY, 2, 0)
    job_id = _job(now)
    runner = _RecordingRunner()

    result = await jobs.run_due_jobs(runner=runner, now=now)

    assert result["submitted"] == [] and runner.submitted == []
    status, defer_until = _job_state(job_id)
    assert status == "deferred" and defer_until.replace(tzinfo=timezone.utc) == _dt(MONDAY, 4, 0)
    payload = [j for j in jobs.list_scheduler_jobs_payload() if j["job_id"] == job_id][0]
    assert "отложена до 04:00" in payload["deferred_note"]


@pytest.mark.anyio
async def test_deferred_job_runs_in_offpeak():
    now = _dt(MONDAY, 2, 0)
    job_id = _job(now)
    runner = _RecordingRunner()
    await jobs.run_due_jobs(runner=runner, now=now)

    result = await jobs.run_due_jobs(runner=runner, now=_dt(MONDAY, 4, 0))

    assert len(result["submitted"]) == 1 and runner.submitted
    status, defer_until = _job_state(job_id)
    assert status == "ok" and defer_until is None


@pytest.mark.anyio
async def test_weekend_and_urgent_run_immediately():
    runner = _RecordingRunner()
    _job(_dt(SATURDAY, 2, 0), name="weekend")
    await jobs.run_due_jobs(runner=runner, now=_dt(SATURDAY, 2, 0))
    assert len(runner.submitted) == 1

    runner2 = _RecordingRunner()
    _job(_dt(MONDAY, 2, 0), urgent=True, name="urgent")
    await jobs.run_due_jobs(runner=runner2, now=_dt(MONDAY, 2, 0))
    assert len(runner2.submitted) == 1


@pytest.mark.anyio
async def test_deferral_capped_at_six_hours(monkeypatch):
    now = _dt(MONDAY, 2, 0)
    _job(now)
    runner = _RecordingRunner()
    monkeypatch.setattr(jobs, "_offpeak_start", lambda _now: now + timedelta(hours=7))

    await jobs.run_due_jobs(runner=runner, now=now)

    assert len(runner.submitted) == 1, "при переносе > 6 часов job запускается сразу"


def test_migration_adds_and_drops_urgency_columns(tmp_path):
    from alembic import command
    from alembic.config import Config
    import sqlalchemy as sa

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["dsn_explicit"] = f"sqlite:///{(tmp_path / 'e8.db').as_posix()}"
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "f1a2b3c4d5e6")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    columns = {c["name"] for c in sa.inspect(engine).get_columns("scheduler_jobs")}
    assert {"urgent", "defer_until"} <= columns

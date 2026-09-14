"""In-process scheduler (APScheduler) that triggers runs on the cron expression from Settings."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from ..config import Env
from ..settings import get_settings
from .llm import SearchClient
from .runner import RunInProgress, execute_run

log = logging.getLogger(__name__)
JOB_ID = "dealwatch-scheduled-run"


class RunScheduler:
    def __init__(self, session_factory: sessionmaker[Session], client_factory: Callable[[], SearchClient], env: Env) -> None:
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._env = env
        self._scheduler = BackgroundScheduler()

    def start(self) -> None:
        self._scheduler.start()
        self.reload()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reload(self) -> None:
        """Re-read schedule settings (called after the settings page is saved)."""
        with self._session_factory() as session:
            settings = get_settings(session)
        if self._scheduler.get_job(JOB_ID):
            self._scheduler.remove_job(JOB_ID)
        if not settings.schedule_enabled:
            log.info("scheduled runs disabled")
            return
        try:
            trigger = CronTrigger.from_crontab(settings.schedule_cron, timezone=settings.timezone)
        except (ValueError, TypeError) as exc:
            log.error("invalid schedule '%s': %s", settings.schedule_cron, exc)
            return
        self._scheduler.add_job(self._job, trigger, id=JOB_ID, replace_existing=True, coalesce=True, misfire_grace_time=3600)
        log.info("scheduled runs: %s (%s), next at %s", settings.schedule_cron, settings.timezone, self.next_run_time())

    def next_run_time(self) -> datetime | None:
        job = self._scheduler.get_job(JOB_ID) if self._scheduler.running else None
        return job.next_run_time if job else None

    def _job(self) -> None:
        try:
            execute_run(self._session_factory, self._client_factory(), env=self._env, trigger="schedule")
        except RunInProgress:
            log.warning("scheduled run skipped: another run is active")
        except Exception:  # keep the scheduler alive
            log.exception("scheduled run failed")

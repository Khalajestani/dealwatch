"""The two jobs inside the web process: the daily price check at the start time from Settings (02:30 by default)
and the nightly backup at 03:00, both in the time zone from Settings. The check is the only scheduled step that
reaches Claude; Ask, the smart entry and Check now run only when used."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from .. import backup
from ..config import Env
from ..settings import Settings, get_settings
from .runner import RunInProgress, execute_run
from .search import SearchClient

log = logging.getLogger(__name__)
CHECK_JOB = "price-check"
BACKUP_JOB = "nightly-backup"
BACKUP_HOUR = 3  # after the 02:30 check has usually finished, before the Pi's own updates and reboot at 04:30


def check_trigger(settings: Settings) -> CronTrigger:
    """Every day at the start time, on the local clock of the Settings time zone (so summer and winter time alike)."""
    hour, minute = map(int, settings.schedule_time.split(":"))
    return CronTrigger(hour=hour, minute=minute, timezone=settings.timezone)


class RunScheduler:
    def __init__(
        self, session_factory: sessionmaker[Session], client_factory: Callable[[], SearchClient | None], env: Env
    ) -> None:
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
        """Re-read the schedule (called after the settings page is saved)."""
        if not self._scheduler.running:
            return
        with self._session_factory() as session:
            settings = get_settings(session)
        self._scheduler.add_job(
            self._backup,
            CronTrigger(hour=BACKUP_HOUR, timezone=settings.timezone),
            id=BACKUP_JOB,
            replace_existing=True,
        )
        if self._scheduler.get_job(CHECK_JOB):
            self._scheduler.remove_job(CHECK_JOB)
        if not settings.schedule_enabled:
            log.info("daily price check is off")
            return
        try:
            trigger = check_trigger(settings)
        except (ValueError, TypeError) as exc:
            log.error("invalid start time '%s': %s", settings.schedule_time, exc)
            return
        # misfire_grace_time: a check missed while the app was down (a deploy, a power cut) runs when it is back within
        # the hour.
        self._scheduler.add_job(
            self._check, trigger, id=CHECK_JOB, replace_existing=True, coalesce=True, misfire_grace_time=3600
        )
        log.info(
            "price check: daily at %s (%s), next at %s", settings.schedule_time, settings.timezone, self.next_run_time()
        )

    def next_run_time(self) -> datetime | None:
        job = self._scheduler.get_job(CHECK_JOB) if self._scheduler.running else None
        return job.next_run_time if job else None

    def _check(self) -> None:
        client = self._client_factory()
        if client is None:
            log.warning("scheduled price check skipped: Claude is not connected")
            return
        try:
            execute_run(self._session_factory, client, trigger="schedule")
        except RunInProgress:
            log.warning("scheduled price check skipped: another one is running")
        except Exception:  # keep the scheduler alive
            log.exception("scheduled price check failed")

    def _backup(self) -> None:
        with self._session_factory() as session:
            timezone = get_settings(session).timezone
        try:
            log.info("nightly backup: %s", backup.backup(self._env, timezone=timezone))
        except Exception:
            log.exception("nightly backup failed")

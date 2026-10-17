"""The daily check starts at one time from Settings (02:30), on the local clock of the Settings time zone."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dealwatch.services.scheduler import BACKUP_JOB, CHECK_JOB, RunScheduler, check_trigger
from dealwatch.settings import Settings, save_settings, validate_setting

AMSTERDAM, UTC = ZoneInfo("Europe/Amsterdam"), ZoneInfo("UTC")


def next_check(settings: Settings, after: str) -> datetime:
    return check_trigger(settings).get_next_fire_time(None, datetime.fromisoformat(after).replace(tzinfo=AMSTERDAM))


def test_the_check_starts_at_0230_amsterdam_in_summer_and_winter_time():
    s = Settings()
    assert (s.schedule_time, s.timezone) == ("02:30", "Europe/Amsterdam")
    summer, winter = next_check(s, "2026-07-01 12:00"), next_check(s, "2026-12-01 12:00")
    assert (
        f"{summer.astimezone(AMSTERDAM):%d/%m %H:%M}" == "02/07 02:30" and f"{summer.astimezone(UTC):%H:%M}" == "00:30"
    )
    assert (
        f"{winter.astimezone(AMSTERDAM):%d/%m %H:%M}" == "02/12 02:30" and f"{winter.astimezone(UTC):%H:%M}" == "01:30"
    )
    assert f"{next_check(s, '2026-10-25 12:00').astimezone(AMSTERDAM):%d/%m %H:%M}" == "26/10 02:30", (
        "after the clocks go back"
    )
    assert f"{next_check(Settings(schedule_time='05:15'), '2026-07-01 12:00'):%H:%M}" == "05:15"


def test_the_start_time_is_hh_mm():
    assert validate_setting("schedule_time", "2:30") == "02:30"
    for wrong in ("25:00", "0 7 * * *", "02:30:00", ""):
        with pytest.raises(ValueError, match="HH:MM"):
            validate_setting("schedule_time", wrong)


def test_only_the_check_and_the_backup_are_scheduled(session_factory, env):
    scheduler = RunScheduler(session_factory, lambda: None, env)
    scheduler.start()
    try:
        assert {job.id for job in scheduler._scheduler.get_jobs()} == {CHECK_JOB, BACKUP_JOB}
        at = scheduler.next_run_time().astimezone(AMSTERDAM)
        assert (at.hour, at.minute) == (2, 30)
        with session_factory() as s:
            assert save_settings(s, {"schedule_time": "03:45"}) == []
        scheduler.reload()  # what saving the settings page does
        at = scheduler.next_run_time().astimezone(AMSTERDAM)
        assert (at.hour, at.minute) == (3, 45)
    finally:
        scheduler.shutdown()

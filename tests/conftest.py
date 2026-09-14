from __future__ import annotations

import pytest

from dealwatch.config import Env
from dealwatch.db import init_db, make_engine, make_session_factory
from dealwatch.seed import seed_retailers, seed_sample_items


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_retailers(session)
        seed_sample_items(session)
    return factory


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(
        data_dir=tmp_path, db_path=tmp_path / "test.db", host="127.0.0.1", port=0, scheduler_enabled=False, fake_llm=True,
        smtp_host=None, smtp_port=587, smtp_user=None, smtp_password=None, smtp_from=None, smtp_to=None,
        telegram_bot_token=None, telegram_chat_id=None,
    )

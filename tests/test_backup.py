import tarfile

import pytest
from sqlalchemy import delete, select

from dealwatch.backup import backup, restore
from dealwatch.db import make_engine, make_session_factory
from dealwatch.models import Item


def test_backup_holds_the_database_and_photos_and_keeps_the_newest(session_factory, env):
    env.uploads_dir.mkdir(parents=True, exist_ok=True)
    (env.uploads_dir / ("a" * 32 + ".jpg")).write_bytes(b"jpeg")
    archives = [backup(env, keep=2, timezone="UTC") for _ in range(3)]
    with tarfile.open(archives[-1]) as tar:
        names = tar.getnames()
    assert "dealwatch.db" in names and f"uploads/{'a' * 32}.jpg" in names
    assert len(list(env.backups_dir.glob("dealwatch-*.tar.gz"))) <= 2


def test_backup_without_a_database_says_so(env):
    with pytest.raises(FileNotFoundError):
        backup(env)


def test_restore_puts_back_the_database_and_photos(session_factory, env, tmp_path):
    photo = env.uploads_dir / ("a" * 32 + ".jpg")
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"jpeg")
    with session_factory() as s:
        names = sorted(s.scalars(select(Item.name)))
    archive = backup(env, timezone="UTC")

    # An incident: every item gone and a lost photo. The restore puts the archive back as it was.
    with session_factory() as s:
        s.execute(delete(Item))
        s.commit()
    photo.unlink()
    before = restore(env, archive)
    with make_session_factory(make_engine(env.db_path))() as s:
        assert sorted(s.scalars(select(Item.name))) == names
    assert photo.exists()
    assert before is not None and before.exists()  # what was there, so the restore can be undone

    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    with pytest.raises(ValueError, match="has no dealwatch"):
        restore(env, empty)

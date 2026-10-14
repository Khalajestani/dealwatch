import tarfile

import pytest

from dealwatch.backup import backup


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

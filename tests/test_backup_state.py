import tarfile

import backup_state
import ibkr_state


def test_backup_uses_sqlite_snapshot_and_excludes_secrets(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    database = project / "mimicry_state.sqlite3"
    connection = ibkr_state.connect(database)
    ibkr_state.set_live_baseline(connection, "U111", 50_000)
    connection.close()
    (project / "browser_state.json").write_text("{}")
    (project / "secrets.env").write_text("DO_NOT_BACK_UP=1")
    monkeypatch.setattr(backup_state, "ROOT", project)
    monkeypatch.setattr(ibkr_state, "STATE_DB_FILE", database)

    archive = backup_state.create_backup(tmp_path / "output")
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "mimicry_state.sqlite3" in names
    assert "browser_state.json" in names
    assert "backup_metadata.json" in names
    assert "secrets.env" not in names

from __future__ import annotations

from backend.store import DatabaseStore


def test_sqlite_store_persists_records_across_restarts(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'persistent.db'}"
    first = DatabaseStore(database_url, seed=False)
    with first.lock:
        user = first.create_user(
            email="persistent@example.com",
            display_name="Persistent User",
            organization_id=None,
            password="test-password",
        )
        interview = first.create_session(
            owner_user_id=user.id,
            title="Persistent interview",
        )
        interview_id = interview.id
    first.close()

    second = DatabaseStore(database_url, seed=False)
    with second.lock:
        restored = second.sessions[interview_id]
        assert restored.title == "Persistent interview"
        assert second.canvases[interview_id].doc == {
            "nodes": [],
            "edges": [],
            "strokes": [],
        }
    second.close()


def test_database_url_environment_variable_configures_store(monkeypatch, tmp_path):
    configured_url = f"sqlite+pysqlite:///{tmp_path / 'configured.db'}"
    monkeypatch.setenv("DATABASE_URL", configured_url)

    store = DatabaseStore(seed=False)

    assert store.database_url == configured_url
    assert store.engine.dialect.name == "sqlite"
    store.close()

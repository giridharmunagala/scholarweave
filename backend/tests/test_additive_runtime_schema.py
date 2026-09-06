from __future__ import annotations

from sqlalchemy import inspect

from backend.persistence.database import create_session_factory


def test_existing_provider_settings_upgrade_without_cutover_or_data_loss(test_settings):
    factory = create_session_factory(test_settings)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE provider_profiles DROP COLUMN config_json")
        connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value_json, updated_at) "
            "VALUES ('keep-this-setting', '\"preserved\"', CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    restored = create_session_factory(test_settings)
    engine = restored.kw["bind"]
    assert "config_json" in {column["name"] for column in inspect(engine).get_columns("provider_profiles")}
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT value_json FROM app_settings WHERE key='keep-this-setting'"
        ).scalar_one() == '"preserved"'
    engine.dispose()
    reopened = create_session_factory(test_settings)
    reopened.kw["bind"].dispose()
    assert len(list(test_settings.database_path.parent.glob("metadata.pre-sdk-*.sqlite3"))) == 1

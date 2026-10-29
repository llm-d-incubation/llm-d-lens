"""Empty Prism initialization discards only explicitly approved legacy SQL data."""

import importlib
import json
import zipfile

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from llm_d_lens.benchmark_rag.publication_contracts import PublicationError
from llm_d_lens.db.base import Base
from llm_d_lens.db.dao.rag_transfer import reset_legacy_rag_storage

migration = importlib.import_module(
    "llm_d_lens.db.migrations.versions.8bf2a6c1d903_structured_public_benchmark_records"
)


@pytest.fixture
def legacy(postgres_engine, postgres_config):
    with postgres_engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE public_benchmark_records"))
    command.stamp(postgres_config, "42a3e9b47726")
    command.upgrade(postgres_config, "df3a4d42cbb7")
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO rag_items (item_id, kind, origin_id, logical_key, source_revision, "
                "content_sha256, access_classification, publication_state, source_path, format, "
                "source_role, source_details) VALUES "
                "('old-source', 'source', 'old-document', 'file', 'revision', :hash, "
                "'verified_public', 'staged', 'document.md', 'markdown', 'file', "
                '\'{"parser_version":"test"}\'::jsonb)'
            ),
            {"hash": "b" * 64},
        )
        connection.execute(sa.text("INSERT INTO rag_search_pointers (mode,generation) VALUES ('lexical',1)"))
    return postgres_engine, postgres_config


def assert_empty_prism_storage(engine):
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM public_benchmark_records")) == 0
        assert not set(migration.LEGACY_TABLES).intersection(sa.inspect(connection).get_table_names())
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_nonempty_cleanup_requires_explicit_approval(legacy):
    engine, config = legacy
    with pytest.raises(RuntimeError, match="explicit discard_legacy"):
        command.upgrade(config, migration.revision)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM rag_items")) == 1
        assert not sa.inspect(connection).has_table("public_benchmark_records")
    config.attributes["discard_legacy_rag"] = True
    command.upgrade(config, migration.revision)
    assert_empty_prism_storage(engine)


def test_existing_prism_data_blocks_empty_table_cleanup(legacy):
    engine, _ = legacy
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE rag_items SET origin_id='prism:public-snapshot'"))
    with pytest.raises(RuntimeError, match="contains Prism"), engine.begin() as connection:
        migration.reset_storage(connection, discard_legacy=True)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM rag_items")) == 1


@pytest.mark.parametrize("managed", [False, True])
def test_operator_backs_up_before_cleanup_without_opensearch(legacy, tmp_path, managed):
    engine, _ = legacy
    if not managed:
        with engine.begin() as connection:
            connection.execute(sa.text("DROP TABLE alembic_version"))
    result = reset_legacy_rag_storage(tmp_path, writers_quiesced=True, engine=engine)
    assert result["discarded_rows"] == {"rag_search_pointers": 1, "rag_search_builds": 0, "rag_items": 1}
    assert result["prism_imported"] is False
    assert result["opensearch_touched"] is False
    assert result["indexes_rebuilt"] is False
    assert_empty_prism_storage(engine)
    with engine.connect() as connection:
        assert sa.inspect(connection).has_table("alembic_version") is managed
    backup = next(tmp_path.glob("*/legacy-sql.zip"))
    with zipfile.ZipFile(backup) as bundle:
        assert json.loads(bundle.read("rag_items.json"))[0]["item_id"] == "old-source"
        assert json.loads(bundle.read("rag_search_pointers.json"))[0]["generation"] == 1
    assert list(tmp_path.glob("*/committed.json"))
    with pytest.raises(PublicationError, match="already exists"):
        reset_legacy_rag_storage(tmp_path, writers_quiesced=True, engine=engine)


def test_new_table_rejects_non_prism_origins(postgres_engine):
    with (
        pytest.raises(IntegrityError, match="ck_public_benchmark_prism_origin"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO public_benchmark_records (item_id, kind, origin_id, logical_key, "
                "source_revision, content_sha256, access_classification, publication_state, "
                "source_path, format, source_role, source_details) VALUES "
                "('not-prism', 'source', 'other-origin', 'file', 'revision', :hash, "
                "'verified_public', 'staged', 'source.json', 'json', 'file', "
                '\'{"parser_version":"test"}\'::jsonb)'
            ),
            {"hash": "b" * 64},
        )

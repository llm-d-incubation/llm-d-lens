"""Cluster plan/execution-lock migration chain and reversible DDL."""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

merge = importlib.import_module("llm_d_lens.db.migrations.versions.e0716e45ba8f_merge_alembic_heads")
migration = importlib.import_module(
    "llm_d_lens.db.migrations.versions.de5cc4aaefa8_add_cluster_plan_and_execution_lock"
)
ledger_migration = importlib.import_module(
    "llm_d_lens.db.migrations.versions.d1cf8c18adc3_add_execution_operations_ledger"
)
completed_hash_migration = importlib.import_module(
    "llm_d_lens.db.migrations.versions.f2391c957d91_add_execution_completed_plan_hash"
)
drop_steps_migration = importlib.import_module("llm_d_lens.db.migrations.versions.a7c9e1f3b5d8_drop_execution_steps")

LEDGER_COLUMN = "execution_operations"
NEW_COLUMNS = {
    "provisioning_plan",
    "execution_token",
    "execution_started_at",
    "execution_heartbeat_at",
    "execution_cancel_requested",
    "execution_operations",
    "execution_completed_at",
    "execution_completed_plan_hash",
}
ORIGINAL_HEADS = ("814f033eb959", "8bf2a6c1d903", "e6b8d0f2a4c7")


def _columns(engine) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(engine).get_columns("clusters")}


def _column_names(engine) -> set[str]:
    return set(_columns(engine))


def _drop_new_columns(engine) -> None:
    with engine.begin() as connection:
        for column in NEW_COLUMNS:
            connection.execute(sa.text(f'ALTER TABLE clusters DROP COLUMN "{column}"'))


def _insert_legacy_cluster(engine, cluster_id: str) -> None:
    """Insert a row shaped like the table before this migration added new columns."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO clusters (id, name, description, kubeconfig, draft, proxy_mode, version_id) "
                "VALUES (:id, :name, '', 'apiVersion: v1', false, 'auto', 1)"
            ),
            {"id": cluster_id, "name": cluster_id},
        )


def test_merge_revision_joins_the_three_heads(postgres_config):
    script = ScriptDirectory.from_config(postgres_config)

    # One head, with the cluster-plan migrations below it; later migrations may extend it.
    (head,) = script.get_heads()
    assert drop_steps_migration.revision in {rev.revision for rev in script.iterate_revisions(head, merge.revision)}
    assert script.get_revision(merge.revision).down_revision == ORIGINAL_HEADS
    assert script.get_revision(migration.revision).down_revision == merge.revision
    assert script.get_revision(ledger_migration.revision).down_revision == migration.revision
    assert script.get_revision(completed_hash_migration.revision).down_revision == ledger_migration.revision
    assert script.get_revision(drop_steps_migration.revision).down_revision == completed_hash_migration.revision


def test_upgrade_adds_and_downgrade_removes_columns(postgres_engine, postgres_config):
    _drop_new_columns(postgres_engine)
    command.stamp(postgres_config, merge.revision)
    assert not (NEW_COLUMNS & _column_names(postgres_engine))
    _insert_legacy_cluster(postgres_engine, "legacy01")

    command.upgrade(postgres_config, drop_steps_migration.revision)
    columns = _columns(postgres_engine)
    assert NEW_COLUMNS.issubset(columns)
    assert columns["provisioning_plan"]["nullable"] is True
    assert columns["execution_started_at"]["nullable"] is True
    assert columns["execution_heartbeat_at"]["nullable"] is True
    assert columns["execution_cancel_requested"]["nullable"] is False
    assert "execution_steps" not in columns
    assert columns["execution_operations"]["nullable"] is True
    assert columns["execution_completed_at"]["nullable"] is True
    default = columns["execution_cancel_requested"]["default"]
    assert default is not None
    assert default.lower() == "false"

    with postgres_engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT provisioning_plan, execution_token, execution_started_at, "
                    "execution_heartbeat_at, execution_cancel_requested, "
                    "execution_completed_at "
                    "FROM clusters WHERE id = :id"
                ),
                {"id": "legacy01"},
            )
            .mappings()
            .one()
        )
    assert row["provisioning_plan"] is None
    assert row["execution_token"] is None
    assert row["execution_started_at"] is None
    assert row["execution_heartbeat_at"] is None
    assert row["execution_cancel_requested"] is False
    assert row["execution_completed_at"] is None

    upgraded = set(columns)
    command.downgrade(postgres_config, merge.revision)
    assert _column_names(postgres_engine) == upgraded - NEW_COLUMNS


def test_execution_operations_column_is_added_and_removed(postgres_engine, postgres_config):
    with postgres_engine.begin() as connection:
        connection.execute(sa.text(f'ALTER TABLE clusters DROP COLUMN "{LEDGER_COLUMN}"'))
    command.stamp(postgres_config, migration.revision)
    assert LEDGER_COLUMN not in _column_names(postgres_engine)
    _insert_legacy_cluster(postgres_engine, "legacy02")

    command.upgrade(postgres_config, ledger_migration.revision)
    columns = _columns(postgres_engine)
    assert LEDGER_COLUMN in columns
    assert columns[LEDGER_COLUMN]["nullable"] is True

    with postgres_engine.connect() as connection:
        value = connection.execute(
            sa.text("SELECT execution_operations FROM clusters WHERE id = :id"),
            {"id": "legacy02"},
        ).scalar_one()
    assert value is None

    command.downgrade(postgres_config, migration.revision)
    assert LEDGER_COLUMN not in _column_names(postgres_engine)


def test_execution_completed_plan_hash_column_is_added_and_removed(postgres_engine, postgres_config):
    column = "execution_completed_plan_hash"
    with postgres_engine.begin() as connection:
        connection.execute(sa.text(f'ALTER TABLE clusters DROP COLUMN "{column}"'))
    command.stamp(postgres_config, ledger_migration.revision)
    assert column not in _column_names(postgres_engine)

    command.upgrade(postgres_config, completed_hash_migration.revision)
    columns = _columns(postgres_engine)
    assert column in columns
    assert columns[column]["nullable"] is True

    command.downgrade(postgres_config, ledger_migration.revision)
    assert column not in _column_names(postgres_engine)


def test_drop_steps_preserves_completion_and_downgrade_restores_steps(postgres_engine, postgres_config):
    _drop_new_columns(postgres_engine)
    command.stamp(postgres_config, merge.revision)
    _insert_legacy_cluster(postgres_engine, "active01")
    command.upgrade(postgres_config, drop_steps_migration.revision)
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE clusters SET execution_operations = :ops, "
                "execution_completed_at = now(), execution_completed_plan_hash = :plan "
                "WHERE id = :id"
            ),
            {
                "ops": '{"gateway.manifests": "user_done"}',
                "plan": "plan-hash",
                "id": "active01",
            },
        )

    command.downgrade(postgres_config, completed_hash_migration.revision)
    assert "execution_steps" in _column_names(postgres_engine)
    with postgres_engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT execution_operations, execution_completed_at, "
                    "execution_completed_plan_hash FROM clusters WHERE id = :id"
                ),
                {"id": "active01"},
            )
            .mappings()
            .one()
        )
    assert row["execution_operations"] == {"gateway.manifests": "user_done"}
    assert row["execution_completed_at"] is not None
    assert row["execution_completed_plan_hash"] == "plan-hash"

    command.upgrade(postgres_config, drop_steps_migration.revision)
    assert "execution_steps" not in _column_names(postgres_engine)
    with postgres_engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT execution_operations, execution_completed_at, "
                    "execution_completed_plan_hash FROM clusters WHERE id = :id"
                ),
                {"id": "active01"},
            )
            .mappings()
            .one()
        )
    assert row["execution_operations"] == {"gateway.manifests": "user_done"}
    assert row["execution_completed_at"] is not None
    assert row["execution_completed_plan_hash"] == "plan-hash"

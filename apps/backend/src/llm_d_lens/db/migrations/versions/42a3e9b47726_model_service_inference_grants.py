"""model service inference grants and retire deployment sharing

Revision ID: 42a3e9b47726
Revises: c21490d948bb
Create Date: 2026-10-17 15:08:05.615428

"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "42a3e9b47726"
down_revision = "c21490d948bb"
branch_labels = None
depends_on = None


def _retire_shares(resource_types: tuple[str, ...]) -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    audit = sa.Table("audit_logs", metadata, autoload_with=connection)
    for table_name, subject_key in (("user_role_bindings", "user_id"), ("group_role_bindings", "group_id")):
        bindings = sa.Table(table_name, metadata, autoload_with=connection)
        condition = sa.and_(bindings.c.scope_type == "resource", bindings.c.scope_resource_type.in_(resource_types))
        for binding in connection.execute(sa.select(bindings).where(condition)).mappings():
            connection.execute(
                audit.insert().values(
                    id=str(uuid4()),
                    actor_username="schema migration",
                    event_type="resource_share_retired",
                    target_type=binding["scope_resource_type"],
                    target_id=binding["scope_resource_id"],
                    cluster_id=binding["scope_cluster_id"],
                    result="success",
                    detail={
                        "migration": revision,
                        "binding_id": binding["id"],
                        "subject_type": "user" if subject_key == "user_id" else "group",
                        "subject_id": binding[subject_key],
                        "role_id": binding["role_id"],
                        "granted_by_user_id": binding["granted_by_user_id"],
                        "reason": "No automatic conversion or restoration of inference grants",
                    },
                )
            )
        connection.execute(bindings.delete().where(condition))


def _replace_permission(removed: str, added: str) -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    permissions = sa.Table("role_permissions", metadata, autoload_with=connection)
    roles = sa.Table("roles", metadata, autoload_with=connection)
    connection.execute(permissions.delete().where(permissions.c.permission == removed))
    for role_id in connection.scalars(
        sa.select(roles.c.id).where(roles.c.is_builtin.is_(True), roles.c.name.in_(("admin", "maintainer")))
    ):
        exists = connection.scalar(
            sa.select(permissions.c.role_id).where(permissions.c.role_id == role_id, permissions.c.permission == added)
        )
        if exists is None:
            connection.execute(permissions.insert().values(role_id=role_id, permission=added))
    connection.execute(sa.text("UPDATE users SET principal_version = principal_version + 1"))
    connection.execute(sa.text("UPDATE groups SET authz_version = authz_version + 1"))


def upgrade() -> None:
    """Add ownership and retire deployment shares without expanding access."""
    with op.batch_alter_table("model_service_groups") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("owner_group_id", sa.String(36), nullable=True))
        batch.create_index("ix_model_service_groups_owner_user_id", ["owner_user_id"])
        batch.create_index("ix_model_service_groups_owner_group_id", ["owner_group_id"])
        batch.create_foreign_key("fk_model_service_owner_user", "users", ["owner_user_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key(
            "fk_model_service_owner_group", "groups", ["owner_group_id"], ["id"], ondelete="SET NULL"
        )
    op.execute("UPDATE model_service_groups SET owner_user_id = created_by_user_id")
    _retire_shares(("deployment_run", "deployment_execution"))
    _replace_permission("deployment:run:share", "model-service:group:share")


def downgrade() -> None:
    """Remove model grants; never resurrect retired deployment shares."""
    _retire_shares(("model_service_group",))
    _replace_permission("model-service:group:share", "deployment:run:share")
    with op.batch_alter_table("model_service_groups") as batch:
        batch.drop_constraint("fk_model_service_owner_user", type_="foreignkey")
        batch.drop_constraint("fk_model_service_owner_group", type_="foreignkey")
        batch.drop_index("ix_model_service_groups_owner_user_id")
        batch.drop_index("ix_model_service_groups_owner_group_id")
        batch.drop_column("owner_user_id")
        batch.drop_column("owner_group_id")

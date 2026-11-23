"""Immutable deployment plans and atomic execution ownership."""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_lens.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_lens.deploy.contracts import DeploymentPreparedPlan


class DeploymentPlanRow(Base, DeclarativeDtoMixin):
    __tablename__ = "deployment_plans"
    dto_type = DeploymentPreparedPlan
    column_map = {
        name: name
        for name in (
            "id",
            "cluster_id",
            "owner_user_id",
            "owner_group_id",
            "digest",
            "payload",
            "preflight",
            "created_at",
            "status",
            "run_id",
        )
    }
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    owner_group_id: Mapped[str | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"), index=True)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    preflight: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version_id}

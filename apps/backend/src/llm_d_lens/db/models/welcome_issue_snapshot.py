"""``welcome_issue_snapshots`` table -- latest Welcome issue composition.

A single row keeps the most recent system-wide snapshot produced by the
background Welcome check. Reads filter its issues per principal; the composer
never runs on the request path once this table has a row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_lens.dashboard.contracts import WelcomeIssueSnapshot
from llm_d_lens.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime


class WelcomeIssueSnapshotRow(Base, DeclarativeDtoMixin):
    __tablename__ = "welcome_issue_snapshots"

    dto_type = WelcomeIssueSnapshot
    column_map = {
        "snapshot_id": "snapshot_id",
        "generated_at": "generated_at",
        "observed_at": "observed_at",
        "state": "state",
        "stale": "stale",
        "coverage_observed": "coverage.observed",
        "coverage_expected": "coverage.expected",
        "coverage_unit": "coverage.unit",
        "issues": "issues",
        "first_seen": "first_seen",
    }

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="current")
    snapshot_id: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unavailable")
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    coverage_observed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_expected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="items")
    issues: Mapped[list] = mapped_column(JSONVariant, nullable=False, default=list)
    first_seen: Mapped[dict] = mapped_column(JSONVariant, nullable=False, default=dict)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}

    @classmethod
    def from_dto(cls, dto: WelcomeIssueSnapshot, **extra_columns):
        row = super().from_dto(dto, **extra_columns)
        row.issues = [issue.model_dump(mode="json", by_alias=True) for issue in dto.issues]
        row.first_seen = {issue_id: value.isoformat() for issue_id, value in dto.first_seen.items()}
        return row

    def sync_from_dto(self, dto: WelcomeIssueSnapshot, **extra_columns) -> None:
        super().sync_from_dto(dto, **extra_columns)
        self.issues = [issue.model_dump(mode="json", by_alias=True) for issue in dto.issues]
        self.first_seen = {issue_id: value.isoformat() for issue_id, value in dto.first_seen.items()}

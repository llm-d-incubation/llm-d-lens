"""Custom model identities and versioned serving defaults."""

from datetime import datetime

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_lens.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_lens.model_catalog.contracts import CatalogEntry


class ModelCatalogEntryRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_catalog_entries"
    dto_type = CatalogEntry
    column_map = {
        name: name
        for name in (
            "id",
            "name",
            "repository",
            "revision",
            "model_type",
            "dtype",
            "weight_gib",
            "context_length",
            "serving_defaults",
            "version",
            "created_at",
            "updated_at",
        )
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    repository: Mapped[str] = mapped_column(String(300), nullable=False)
    revision: Mapped[str] = mapped_column(String(200), nullable=False)
    model_type: Mapped[str] = mapped_column(String(100), nullable=False)
    dtype: Mapped[str] = mapped_column(String(40), nullable=False)
    weight_gib: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    serving_defaults: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __mapper_args__ = {"version_id_col": version}

    @classmethod
    def from_dto(cls, dto, **extra_columns):
        return super().from_dto(dto, serving_defaults=dto.serving_defaults.model_dump(), **extra_columns)

    def sync_from_dto(self, dto, **extra_columns):
        super().sync_from_dto(dto, serving_defaults=dto.serving_defaults.model_dump(), **extra_columns)

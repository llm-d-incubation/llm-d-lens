"""Versioned storage contracts; these are not public query responses."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ItemKind = Literal["source", "experiment", "measurement", "passage", "figure", "figure_link"]
PublicationState = Literal["staged", "published", "retired", "tombstone"]
SourceRole = Literal["registry", "manifest", "file"]
Coverage = Literal["complete", "partial", "summary_only", "unknown"]

_COMMON_FIELDS = {
    "item_id",
    "kind",
    "origin_id",
    "logical_key",
    "source_revision",
    "content_sha256",
    "access_classification",
    "publication_state",
    "parent_id",
    "source_path",
    "source_url",
    "format",
}
KIND_FIELDS: dict[str, set[str]] = {
    "source": {"source_role", "source_details", "manifest_item_ids", "published_manifest_id"},
    "experiment": {
        "model",
        "hardware",
        "runtime",
        "workload",
        "configuration",
        "timestamp",
        "timestamp_kind",
        "source_pointer",
    },
    "measurement": {
        "experiment_id",
        "metric",
        "statistic",
        "value",
        "unit",
        "original_value",
        "original_unit",
        "normalized_value",
        "normalized_unit",
        "normalization_rule",
        "series",
        "source_pointer",
        "evidence_type",
    },
    "passage": {"passage_text", "heading_path", "source_pointer"},
    "figure": {"caption", "coverage", "missing_series", "source_pointer"},
    "figure_link": {"referenced_item_id", "source_pointer"},
}
REQUIRED_FIELDS: dict[str, set[str]] = {
    "source": {"source_role", "source_details"},
    "experiment": {"source_pointer"},
    "measurement": {"experiment_id", "metric", "value", "original_value", "source_pointer", "evidence_type"},
    "passage": {"passage_text", "heading_path", "source_pointer"},
    "figure": {"coverage", "source_pointer"},
    "figure_link": {"parent_id", "referenced_item_id", "source_pointer"},
}


class RagItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    item_id: str = Field(min_length=1, max_length=64)
    kind: ItemKind
    origin_id: str = Field(min_length=1)
    logical_key: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_classification: Literal["verified_public"]
    publication_state: PublicationState = "staged"
    parent_id: str | None = None
    source_path: str = Field(min_length=1)
    source_url: str | None = None
    format: str = Field(min_length=1)

    source_role: SourceRole | None = None
    source_details: dict[str, JsonValue] | None = None
    manifest_item_ids: list[str] | None = None
    published_manifest_id: str | None = None

    experiment_id: str | None = None
    model: str | None = None
    hardware: str | None = None
    runtime: str | None = None
    workload: dict[str, JsonValue] | None = None
    configuration: dict[str, JsonValue] | None = None
    timestamp: datetime | None = None
    timestamp_kind: Literal["experiment", "submission", "publication"] | None = None
    source_pointer: dict[str, JsonValue] | None = None

    metric: str | None = Field(default=None, min_length=1)
    statistic: str | None = Field(default=None, min_length=1)
    value: Decimal | None = None
    unit: str | None = Field(default=None, min_length=1)
    original_value: Decimal | None = None
    original_unit: str | None = Field(default=None, min_length=1)
    normalized_value: Decimal | None = None
    normalized_unit: str | None = None
    normalization_rule: str | None = None
    series: dict[str, JsonValue] | None = None
    evidence_type: Literal["raw_json", "published_table"] | None = None

    passage_text: str | None = Field(default=None, min_length=1)
    heading_path: list[str] | None = None
    caption: str | None = None
    coverage: Coverage | None = None
    missing_series: list[str] | None = None
    referenced_item_id: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        present = {name for name in type(self).model_fields if getattr(self, name) is not None}
        forbidden = present - _COMMON_FIELDS - KIND_FIELDS[self.kind]
        missing = REQUIRED_FIELDS[self.kind] - present
        if forbidden or missing:
            raise ValueError(f"{self.kind}: forbidden fields {sorted(forbidden)}; missing fields {sorted(missing)}")
        if self.timestamp is not None and self.timestamp.utcoffset() is None:
            raise ValueError("timestamp requires a timezone")
        if (self.timestamp is None) != (self.timestamp_kind is None):
            raise ValueError("timestamp and timestamp_kind must be supplied together")
        if self.item_id in (self.parent_id, self.experiment_id, self.referenced_item_id, self.published_manifest_id):
            raise ValueError("an item cannot reference itself")
        if self.source_role == "manifest":
            if self.manifest_item_ids is None or len(set(self.manifest_item_ids)) != len(self.manifest_item_ids):
                raise ValueError("a manifest requires unique versioned item IDs")
        elif self.manifest_item_ids is not None:
            raise ValueError("only a manifest can carry membership")
        if self.published_manifest_id is not None and self.source_role != "registry":
            raise ValueError("only a registry can point to a published manifest")
        if self.kind == "measurement":
            if self.value != self.original_value or self.unit != self.original_unit:
                raise ValueError("value/unit must preserve the original; use normalized fields for conversions")
            normalized = (self.normalized_value, self.normalized_unit, self.normalization_rule)
            if any(part is not None for part in normalized) and (
                self.value is None or self.unit is None or normalized != normalize_unit(self.value, self.unit)
            ):
                raise ValueError("normalization requires an explicit supported unit conversion")
        return self


# Denominators remain distinct: output tokens/s is not total tokens/s.
_UNIT_RULES: dict[str, tuple[Decimal, str]] = {
    "s": (Decimal("1000"), "ms"),
    "ms": (Decimal("1"), "ms"),
    "us": (Decimal("0.001"), "ms"),
    "output tokens/s": (Decimal("1"), "output tokens/s"),
    "tokens/s": (Decimal("1"), "tokens/s"),
    "requests/s": (Decimal("1"), "requests/s"),
}
_STATISTIC_ALIASES = {"avg": "mean", "median": "p50"}


def normalize_unit(value: Decimal, unit: str) -> tuple[Decimal, str, str] | None:
    if not value.is_finite():
        raise ValueError("measurement must be finite")
    rule = _UNIT_RULES.get(unit)
    if rule is None:
        return None
    factor, normalized_unit = rule
    with localcontext() as context:
        context.prec = max(context.prec, len(value.as_tuple().digits) + len(factor.as_tuple().digits))
        normalized = value * factor
    return normalized, normalized_unit, f"unit-v1:{unit}->{normalized_unit}"


def normalize_statistic(statistic: str | None) -> str | None:
    """Map only explicit aliases; missing and unknown statistics remain unchanged."""
    return _STATISTIC_ALIASES.get(statistic, statistic) if statistic is not None else None

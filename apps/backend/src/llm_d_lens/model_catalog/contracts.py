"""Validated catalog records and serving settings shared by all deployment callers."""

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

MANAGED_ARGUMENTS = {
    "model",
    "served-model-name",
    "host",
    "port",
    "tensor-parallel-size",
    "pipeline-parallel-size",
    "data-parallel-size",
    "max-model-len",
    "max-num-seqs",
    "max-num-batched-tokens",
    "gpu-memory-utilization",
}


class CatalogContract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", str_strip_whitespace=True
    )


class ServingDefaults(CatalogContract):
    tool_calling: bool = False
    tool_parser: str = Field(default="", max_length=100, pattern=r"^[a-z0-9_]*$")
    reasoning_parser: str = Field(default="", max_length=100, pattern=r"^[a-z0-9_]*$")
    chat_template: str = Field(default="", max_length=8192)
    dtype: str = Field(default="", max_length=40)
    extra: str = Field(default="", max_length=16384)

    @model_validator(mode="after")
    def validate_settings(self):
        serving_arguments(self)
        return self


def serving_arguments(settings: ServingDefaults, overrides: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    values = {}
    for field, name in (
        (settings.reasoning_parser, "reasoning-parser"),
        (settings.chat_template, "chat-template"),
        (settings.dtype, "dtype"),
    ):
        if field:
            values[name] = field
    enabled, parser = settings.tool_calling, settings.tool_parser
    rows = []
    for line in settings.extra.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"(?:--)?([a-z0-9][a-z0-9-]*)(?:=|\s+)(\S.*)", line.strip())
        if not match:
            raise ValueError("Additional arguments must be written as name=value, one per line.")
        rows.append({"name": match[1], "value": match[2].strip()})
    rows.extend(overrides or [])
    for item in rows:
        name, value = item.get("name", ""), item.get("value", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not isinstance(value, str):
            raise ValueError("Invalid serving argument.")
        if name in MANAGED_ARGUMENTS or name == "revision":
            raise ValueError(f"--{name} is managed by the deployment or model revision.")
        if name in {"enable-auto-tool-choice", "no-enable-auto-tool-choice"}:
            if value not in {"true", "false"}:
                raise ValueError(f"--{name} must be true or false.")
            enabled = (value == "true") != (name == "no-enable-auto-tool-choice")
        elif name == "tool-call-parser":
            parser = value
        else:
            values[name] = value
    # Guide shell invocations cannot carry literal newlines, even quoted.
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Serving arguments must be single-line; use a file path for multi-line chat templates.")
    if enabled and not re.fullmatch(r"[a-z0-9_]+", parser):
        raise ValueError("Tool calling requires a valid tool call parser.")
    values["enable-auto-tool-choice"] = "true" if enabled else "false"
    if enabled:
        values["tool-call-parser"] = parser
    return [{"name": name, "value": value} for name, value in values.items()]


class CatalogInput(CatalogContract):
    name: str = Field(min_length=1, max_length=200)
    repository: str = Field(max_length=300, pattern=r"^[\w.-]+/[\w.-]+$")
    revision: str = Field(default="main", min_length=1, max_length=200)
    model_type: str = Field(default="text-generation", min_length=1, max_length=100)
    dtype: str = Field(default="BF16", min_length=1, max_length=40)
    weight_gib: float | None = Field(default=None, alias="weightGiB", gt=0, allow_inf_nan=False)
    context_length: int | None = Field(default=None, ge=256, le=1_048_576)
    serving_defaults: ServingDefaults = Field(default_factory=ServingDefaults)


class CatalogEntry(CatalogInput):
    id: str
    version: int = Field(default=1, ge=1)
    source: str = "custom"
    release_date: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CatalogUpdate(CatalogInput):
    version: int = Field(ge=1)

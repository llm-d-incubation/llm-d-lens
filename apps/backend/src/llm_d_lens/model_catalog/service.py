"""Catalog persistence, built-in seeds and deployment default resolution."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from llm_d_lens.db.dao.model_catalog_entry import ModelCatalogEntryDao

from .contracts import CatalogEntry, CatalogInput, CatalogUpdate, ServingDefaults, serving_arguments


@lru_cache(maxsize=1)
def builtin_entries() -> tuple[CatalogEntry, ...]:
    root = Path(__file__).resolve().parents[5] / "packages/shared/src/catalog"
    metadata = json.loads((root / "modelCatalogMetadata.json").read_text())
    entries = []
    for item in json.loads((root / "modelCatalogEntries.json").read_text()):
        details = metadata.get(item["repository"], {})
        entries.append(
            CatalogEntry.model_validate(
                {
                    **item,
                    "id": re.sub(r"[^a-z0-9]+", "-", item["repository"].lower()).strip("-"),
                    "name": item["repository"].split("/")[-1],
                    "source": "builtin",
                    "weightGiB": details.get("weightGiB"),
                    "contextLength": details.get("contextLength"),
                    "createdAt": "1970-01-01T00:00:00Z",
                    "updatedAt": "1970-01-01T00:00:00Z",
                }
            )
        )
    return tuple(entries)


def list_entries() -> list[CatalogEntry]:
    return [*builtin_entries(), *ModelCatalogEntryDao().list()]


def get_entry(entry_id: str) -> CatalogEntry | None:
    return next((entry for entry in builtin_entries() if entry.id == entry_id), None) or ModelCatalogEntryDao().get(
        entry_id
    )


def create_entry(payload: CatalogInput) -> CatalogEntry:
    return ModelCatalogEntryDao().create(CatalogEntry(id=f"custom-{uuid4()}", **payload.model_dump()))


def update_entry(entry_id: str, payload: CatalogUpdate) -> CatalogEntry:
    entry = get_entry(entry_id)
    if not entry:
        raise ValueError("Model catalog entry not found.")
    if entry.source == "builtin":
        raise ValueError("Built-in models are read-only. Copy this model to customize its defaults.")
    return ModelCatalogEntryDao().save(
        CatalogEntry(
            **payload.model_dump(),
            id=entry_id,
            created_at=entry.created_at,
            updated_at=datetime.now(UTC),
        )
    )


def delete_entry(entry_id: str, version: int) -> None:
    entry = get_entry(entry_id)
    if not entry or entry.source == "builtin":
        raise ValueError("Only existing custom models can be deleted.")
    ModelCatalogEntryDao().delete(entry_id, version)


def resolve_deployment_model(request):
    """Resolve once on creation; persisted requests retain their immutable snapshot."""
    entry_id = request.model_catalog_id
    entry = get_entry(entry_id) if entry_id else None
    if not entry:
        if entry_id:
            raise ValueError("Model catalog entry no longer exists. Choose a model again.")
        if request.serving_overrides is not None:
            settings = ServingDefaults.model_validate(request.serving_overrides)
            return request.model_copy(update={"vllm_arguments": serving_arguments(settings, request.vllm_arguments)})
        return request
    if entry.repository != request.model:
        raise ValueError("Catalog model does not match the requested repository.")
    if request.model_catalog_version is not None and request.model_catalog_version != entry.version:
        raise ValueError("Model defaults changed. Reload the catalog and review the updated defaults.")
    settings = ServingDefaults.model_validate(
        {
            **entry.serving_defaults.model_dump(by_alias=True),
            **(request.serving_overrides or {}),
        }
    )
    arguments = serving_arguments(settings, request.vllm_arguments)
    if entry.revision != "main":
        arguments.append({"name": "revision", "value": entry.revision})
    updates = {
        "model_catalog_id": entry.id,
        "model_catalog_version": entry.version,
        "model_revision": entry.revision,
        "model_catalog_snapshot": entry.model_dump(mode="json", by_alias=True),
        "vllm_arguments": arguments,
    }
    if hasattr(request, "planning_facts") and entry.weight_gib:
        updates["planning_facts"] = replace(request.planning_facts, model_weight_gib=entry.weight_gib)
    if "max_model_len" not in request.model_fields_set and entry.context_length:
        updates["max_model_len"] = entry.context_length
    return request.model_copy(update=updates)

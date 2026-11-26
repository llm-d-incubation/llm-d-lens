"""Authenticated model catalog management."""

from fastapi import APIRouter, Query

from llm_d_lens.api.problems import problem
from llm_d_lens.db.dao.base import ConcurrentUpdateError

from . import service
from .contracts import CatalogEntry, CatalogInput, CatalogUpdate

router = APIRouter(prefix="/api/v1/model-catalog", tags=["model-catalog"])


@router.get(
    "",
    operation_id="list_model_catalog",
    summary="List built-in and custom models",
    description="List model identities, revisions and serving defaults for deployments and model caches.",
)
def list_models() -> dict:
    return {"items": [item.model_dump(mode="json", by_alias=True) for item in service.list_entries()]}


@router.post(
    "",
    status_code=201,
    response_model=CatalogEntry,
    operation_id="create_catalog_model",
    summary="Add a custom model",
    description="Save a custom model identity and validated serving defaults for future deployments.",
)
def create_model(payload: CatalogInput):
    return service.create_entry(payload)


@router.put(
    "/{entry_id}",
    response_model=CatalogEntry,
    operation_id="update_catalog_model",
    summary="Update a custom model",
    description="Replace versioned custom model defaults. Existing deployment snapshots are unchanged.",
)
def update_model(entry_id: str, payload: CatalogUpdate):
    try:
        return service.update_entry(entry_id, payload)
    except ConcurrentUpdateError as error:
        return problem(409, "Model changed", str(error), "model_catalog_conflict")
    except ValueError as error:
        return problem(422, "Invalid model update", str(error), "model_catalog_invalid")


@router.delete(
    "/{entry_id}",
    operation_id="delete_catalog_model",
    summary="Delete a custom model",
    description="Delete a versioned custom entry. Retain model weights and existing deployments.",
)
def delete_model(entry_id: str, version: int = Query(ge=1)):
    try:
        service.delete_entry(entry_id, version)
        return {"deleted": entry_id}
    except ConcurrentUpdateError as error:
        return problem(409, "Model changed", str(error), "model_catalog_conflict")
    except ValueError as error:
        return problem(422, "Invalid model deletion", str(error), "model_catalog_invalid")

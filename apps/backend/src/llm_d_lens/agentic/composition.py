"""Bounded source-optimization selection; rendering remains owned by the composer."""

import json
import logging
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field

from llm_d_lens.ai_providers.client import client_for

from .planner import OpenAICompatiblePlanner

logger = logging.getLogger(__name__)
COMPOSITION_OBJECTIVE = "Meet the explicit SLA first, then minimize accelerator count and resource cost."


class CompositionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(max_length=300)
    guide: str = Field(max_length=200)
    detail: str = Field(default="", max_length=4000)


class CompositionSelectionRequest(BaseModel):
    options: list[CompositionOption] = Field(max_length=200)


class CompositionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: list[str]
    rationale: str


def cost_order(candidates):
    """Keep unknown evidence explicit and never prefer a known SLA failure for savings."""
    tier = {"satisfied": 0, "unknown": 1, "not_satisfied": 2}
    return sorted(
        (candidate for candidate in candidates if candidate.deployable and candidate.slo_status != "not_satisfied"),
        key=lambda candidate: (
            tier[candidate.slo_status],
            candidate.required_gpus,
            -(candidate.score or 0),
            candidate.id,
        ),
    )


async def select_composition_optimizations(run, request: CompositionSelectionRequest) -> dict:
    """AI proposes existing option IDs only; the composer independently validates the combination."""
    ids = [option.id for option in request.options]
    if len(set(ids)) != len(ids):
        raise ValueError("Composition option IDs must be unique")
    if run.request.composition_mode == "advanced":
        return {
            "enabled": ids,
            "rationale": "Enable every compatible optimization, resolving conflicts by SLA and cost.",
            "source": "maximal-compatible",
        }
    provider = OpenAICompatiblePlanner.from_provider_id(run.request.ai_provider_id)
    if provider is not None and ids:
        try:
            client = client_for(**provider.settings.model_dump(mode="json"))
            system_prompt = " ".join(
                (
                    "Select source-backed optimizations for the supplied deployment.",
                    COMPOSITION_OBJECTIVE,
                    "Enable only optimizations useful to this model, workload and explicit user preference.",
                    "Do not add multimodal, asynchronous ingress or a different API protocol to a text streaming "
                    "workload unless requested. Never invent availability, performance measurements or prerequisites.",
                    "The server will validate dependencies, conflicts, resources and Kubernetes admission.",
                    "Return JSON with enabled (unique IDs from options) and rationale (a concise explanation "
                    "of both chosen and omitted optimizations).",
                )
            )
            content = await client.complete(
                system_prompt=system_prompt,
                user_content=json.dumps(
                    {
                        "requirements": asdict(run.request.planning_facts),
                        "preference": run.request.planner_prompt,
                        "candidate": run.selected_candidate.model_dump(mode="json"),
                        "options": [option.model_dump() for option in request.options],
                    }
                ),
                json_schema={
                    "name": "composition_selection",
                    "strict": True,
                    "schema": CompositionSelection.model_json_schema(),
                },
                max_tokens=8192,
            )
            selected = CompositionSelection.model_validate_json(content)
            if len(selected.enabled) != len(set(selected.enabled)) or not set(selected.enabled) <= set(ids):
                raise ValueError("AI selected unknown or duplicate optimization IDs")
            return {**selected.model_dump(), "source": "ai", "fallback_reason": None}
        except Exception as error:
            logger.warning("Composition selection fell back: %s", type(error).__name__)
            fallback_reason = (
                "AI selection unavailable or invalid; retained the validated topology and its source defaults."
            )
    else:
        fallback_reason = "No AI provider configured; retained the validated topology and its source defaults."
    return {"enabled": [], "rationale": fallback_reason, "source": "deterministic", "fallback_reason": fallback_reason}

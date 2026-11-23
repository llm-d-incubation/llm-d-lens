"""Append-only JSON audit snapshots for Agentic planning decisions."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from llm_d_lens.ai_providers.client import AIRequestRecord, capture_ai_requests
from llm_d_lens.infrastructure.hostos import current_host

from .models import AgenticDeploymentRun, AgenticDeploymentStatus
from .planner import PlanningFacts


@dataclass(frozen=True)
class PlanningSnapshotReference:
    snapshot_id: str
    relative_path: str


class PlanningSnapshotStore:
    """Persist complete planning inputs and provider exchanges outside the database."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = base_dir or os.getenv("AGENTIC_PLANNING_SNAPSHOT_DIR")
        self._base_dir = Path(root) if root else Path(__file__).parents[2] / "session-artifacts" / "agentic-planning"

    def save(self, run: AgenticDeploymentRun, facts: PlanningFacts, *, event: str) -> PlanningSnapshotReference:
        snapshot_id = str(uuid4())
        created_at = datetime.now(UTC)
        directory = self._base_dir / run.id
        filename = f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{snapshot_id}.json"
        destination = directory / filename
        payload = self._payload(snapshot_id, created_at, run, facts, event)
        self._write_json(destination, payload)
        return PlanningSnapshotReference(
            snapshot_id=snapshot_id, relative_path=str(destination.relative_to(self._base_dir))
        )

    @contextmanager
    def capture_requests(self, run_id: str, stage: Literal["generator", "planner"]) -> Iterator[list[AIRequestRecord]]:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self._base_dir / run_id / "requests" / f"{timestamp}-{stage}-{uuid4()}.json"

        def persist(records: list[AIRequestRecord]) -> None:
            self._write_json(
                destination,
                {
                    "schema_version": 2,
                    "run_id": run_id,
                    "stage": stage,
                    "requests": [record.model_dump(mode="json") for record in records],
                },
            )

        with capture_ai_requests(persist) as records:
            yield records

    def interrupted_optimizations(self):
        """Recover interrupted tasks for explicit cleanup, never silently redeploy them."""
        if not self._base_dir.is_dir():
            return []
        result = []
        for directory in self._base_dir.iterdir():
            if not directory.is_dir():
                continue
            snapshots = sorted(directory.glob("*.json"), reverse=True)
            if not snapshots:
                continue
            try:
                payload = json.loads(snapshots[0].read_text())
            except (OSError, ValueError):
                logging.getLogger(__name__).exception("Unable to read optimization snapshot %s", snapshots[0].name)
                continue
            saved = payload.get("optimization_run")
            if saved and (saved.get("optimization") or {}).get("status") in {
                "running",
                "cleanup_failed",
                "interrupted",
            }:
                run = AgenticDeploymentRun.model_validate(saved)
                run.optimization["status"] = "interrupted"
                run.optimization["error"] = (
                    "The service stopped during testing. Clean up the recorded test deployments before starting again."
                )
                run.status = AgenticDeploymentStatus.FAILED
                result.append(run)
        return result

    def load_run(self, run_id: str) -> AgenticDeploymentRun | None:
        try:
            UUID(run_id)
        except ValueError:
            return None
        directory = self._base_dir / run_id
        snapshots = sorted(directory.glob("*.json"), reverse=True)
        if not snapshots:
            return None
        payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
        saved = payload.get("workflow_run") or payload.get("optimization_run")
        if saved is None:
            return None
        run = AgenticDeploymentRun.model_validate(saved)
        if run.status == AgenticDeploymentStatus.OPTIMIZING:
            run.status = AgenticDeploymentStatus.FAILED
            run.error = "Testing was interrupted. Clean up the recorded trial resources before retrying."
        return run

    @staticmethod
    def _write_json(destination: Path, payload: dict[str, object]) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                current_host().restrict_to_owner(temporary)
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _payload(
        snapshot_id: str,
        created_at: datetime,
        run: AgenticDeploymentRun,
        facts: PlanningFacts,
        event: str,
    ) -> dict[str, object]:
        request = run.request.model_dump(mode="json")
        request["planner_prompt_present"] = bool(run.request.planner_prompt)
        return {
            "schema_version": 2,
            "snapshot_id": snapshot_id,
            "created_at": created_at.isoformat(),
            "event": event,
            "run_id": run.id,
            "workflow_run": run.model_dump(mode="json") if run.request.requirements is not None else None,
            "request": request,
            "resolved_planning_facts": TypeAdapter(PlanningFacts).dump_python(facts, mode="json"),
            "cluster_snapshot": run.planning_cluster_snapshot,
            "planning_evidence": [evidence.model_dump(mode="json") for evidence in run.planning_evidence],
            "candidate_catalog": [candidate.model_dump(mode="json") for candidate in run.candidates],
            "optimization": run.optimization,
            "optimization_run": run.model_dump(mode="json") if run.optimization else None,
            "selected_candidate": run.selected_candidate.model_dump(mode="json"),
            "decision": None if run.decision is None else run.decision.model_dump(mode="json"),
            "decision_evidence_ids": run.decision_evidence_ids,
            "decision_metadata": None
            if run.decision_metadata is None
            else run.decision_metadata.model_dump(mode="json"),
            "generator": {
                "name": run.generator,
                "model": run.generator_model,
                "fallback_reason": run.generator_fallback_reason,
                "fallback_detail": run.generator_fallback_detail,
                "candidate_proposals": run.generator_candidate_proposals,
                "rejected_candidates": [candidate.model_dump(mode="json") for candidate in run.rejected_candidates],
                "tool_trace": run.generator_tool_trace,
                "requests": [record.model_dump(mode="json") for record in run.generator_requests],
            },
            "planner": {
                "name": run.planner,
                "fallback_reason": run.planner_fallback_reason,
                "requests": [record.model_dump(mode="json") for record in run.planner_requests],
            },
        }

"""Concrete Simulation response-model contracts."""

from __future__ import annotations

from fastapi.routing import APIRoute

from llm_d_lens.simulation import router as simulation_router
from llm_d_lens.simulation.models import (
    SimulationBackendsResponse,
    SimulationModelDiscoveryResponse,
    SimulationResponseCodeIssuePage,
    SimulationScenariosResponse,
    SimulationTask,
    SimulationTaskActionResponse,
    SimulationTaskListPage,
    SimulationTaskQueuedResponse,
    TraceDatasetCatalogResponse,
    TraceDatasetDownloadResponse,
    TraceDatasetTimelineResponse,
)

EXPECTED_RESPONSE_MODELS = {
    ("GET", "/api/v1/simulation/backends"): SimulationBackendsResponse,
    ("POST", "/api/v1/simulation/models/discover"): SimulationModelDiscoveryResponse,
    ("GET", "/api/v1/simulation/scenarios"): SimulationScenariosResponse,
    ("GET", "/api/v1/simulation/trace-datasets"): TraceDatasetCatalogResponse,
    ("POST", "/api/v1/simulation/trace-datasets/download"): TraceDatasetDownloadResponse,
    ("GET", "/api/v1/simulation/trace-datasets/{dataset_name}/timeline"): TraceDatasetTimelineResponse,
    ("GET", "/api/v1/simulation/tasks"): SimulationTaskListPage,
    ("POST", "/api/v1/simulation/tasks"): SimulationTaskQueuedResponse,
    ("GET", "/api/v1/simulation/tasks/{task_id}"): SimulationTask,
    ("GET", "/api/v1/simulation/tasks/{task_id}/response-code-issues"): SimulationResponseCodeIssuePage,
    ("POST", "/api/v1/simulation/tasks/{task_id}/stop"): SimulationTaskActionResponse,
    ("POST", "/api/v1/simulation/tasks/{task_id}/rerun"): SimulationTaskQueuedResponse,
    ("DELETE", "/api/v1/simulation/tasks/{task_id}"): SimulationTaskActionResponse,
}


def test_simulation_data_routes_declare_concrete_response_models():
    declared = {}
    for route in simulation_router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            declared[(method, route.path)] = route.response_model

    assert EXPECTED_RESPONSE_MODELS.items() <= declared.items()


def test_artifact_download_remains_streaming_without_response_model():
    route = next(
        route
        for route in simulation_router.routes
        if isinstance(route, APIRoute) and route.path == "/api/v1/simulation/tasks/{task_id}/artifacts/{kind}"
    )
    assert route.response_model is None


def test_candidate_search_compatibility_routes_declare_concrete_response_models():
    from llm_d_lens.simulation import candidate_search
    from llm_d_lens.simulation.models import (
        CandidateRelaxedResponse,
        CandidateSearchResponse,
        CandidateSupportResponse,
    )

    models = {
        ("POST", "/api/v1/candidate-search"): CandidateSearchResponse,
        ("POST", "/api/v1/candidate-search/relaxed"): CandidateRelaxedResponse,
        ("POST", "/api/v1/candidate-support"): CandidateSupportResponse,
    }
    for route in candidate_search.router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) in models:
                assert route.response_model is models[(method, route.path)]

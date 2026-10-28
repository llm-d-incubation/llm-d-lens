"""Concrete deployment-monitoring response-model contracts."""

from __future__ import annotations

from fastapi.routing import APIRoute

from llm_d_lens.monitoring.deployment import router as deployment_router
from llm_d_lens.monitoring.deployment.models import (
    DeploymentMonitoringListResponse,
    DeploymentMonitoringStatusResponse,
)

STATUS_PAYLOAD = {
    "execution_id": "execution-1",
    "namespace": "workloads",
    "cluster_reachable": True,
    "stack_ready": True,
    "status": "enabled",
    "enabled": True,
    "epp_service": {
        "name": "qwen-epp",
        "labels": {"app.kubernetes.io/name": "qwen-epp"},
        "service_account": "qwen-epp",
    },
    "servicemonitors": ["llm-d-prism-epp-monitor"],
    "podmonitors": ["llm-d-prism-modelserver-monitor"],
    "message": "monitoring enabled",
}


def test_status_payload_validates_as_a_concrete_model():
    status = DeploymentMonitoringStatusResponse.model_validate(STATUS_PAYLOAD)

    assert status.execution_id == "execution-1"
    assert status.epp_service is not None
    assert status.epp_service.name == "qwen-epp"
    assert status.servicemonitors == ["llm-d-prism-epp-monitor"]


def test_cluster_payload_validates_as_a_concrete_model():
    response = DeploymentMonitoringListResponse.model_validate(
        {
            "cluster_id": "cluster-1",
            "cluster_reachable": True,
            "stack_ready": True,
            "items": [STATUS_PAYLOAD],
        }
    )

    assert response.cluster_id == "cluster-1"
    assert isinstance(response.items[0], DeploymentMonitoringStatusResponse)


def test_deployment_monitoring_routes_declare_concrete_response_models():
    models = {
        ("GET", "/api/v1/monitoring/deployments"): DeploymentMonitoringListResponse,
        ("GET", "/api/v1/monitoring/deployments/{execution_id}"): DeploymentMonitoringStatusResponse,
        ("POST", "/api/v1/monitoring/deployments/{execution_id}/{action}"): DeploymentMonitoringStatusResponse,
    }
    for route in deployment_router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) in models:
                assert route.response_model is models[(method, route.path)]

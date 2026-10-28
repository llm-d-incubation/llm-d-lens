"""Tests for the pinned llm-d stack profile."""

from llm_d_lens import versions


def test_router_coordinates_derive_from_one_version():
    current = versions.stack()

    assert versions.router_chart_version() == current.llm_d_router
    assert versions.router_epp_image().endswith(f":{current.llm_d_router}")
    assert versions.router_pd_sidecar_image().endswith(f":{current.llm_d_router}")


def test_min_k8s_version_gate():
    assert versions.min_k8s_version().startswith("v")
    assert versions.k8s_version_supports(versions.min_k8s_version())
    assert versions.k8s_version_supports("1.34.12")
    assert not versions.k8s_version_supports("1.29.15")


def test_describe_reports_the_pinned_inputs():
    payload = versions.describe()

    assert payload["llm_d"] == versions.stack().llm_d
    assert payload["llm_d_router"] == versions.stack().llm_d_router
    assert payload["gateway_providers"]["istio"]
    assert payload["llm_d_inference_payload_processor"]
    assert payload["k8s_lws"] == versions.lws_version()
    assert "resolved" not in payload


def test_api_route_and_service_return_the_concrete_versions_model():
    from llm_d_lens.versions.models import SupportedVersionsResponse
    from llm_d_lens.versions.router import router

    route = next(route for route in router.routes if route.path == "/api/v1/versions")
    assert route.response_model is SupportedVersionsResponse
    response = versions.describe_response()
    assert isinstance(response, SupportedVersionsResponse)
    assert response.model_dump() == versions.describe()

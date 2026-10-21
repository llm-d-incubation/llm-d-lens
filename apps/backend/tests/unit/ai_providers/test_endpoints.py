"""Provider base URL normalization tests."""

from llm_d_lens.ai_providers.endpoints import normalize_provider_base_url


def test_base_url_normalization_accepts_optional_v1_suffix():
    for value in (
        "https://api.example.com",
        "https://api.example.com/",
        "https://api.example.com/v1",
        "https://api.example.com/v1/",
    ):
        assert normalize_provider_base_url(value) == "https://api.example.com/"


def test_base_url_normalization_preserves_non_version_paths():
    assert normalize_provider_base_url("https://api.example.com/openai/v1.0") == "https://api.example.com/openai/v1.0/"
    assert normalize_provider_base_url("https://api.deepseek.com/anthropic") == "https://api.deepseek.com/anthropic/"

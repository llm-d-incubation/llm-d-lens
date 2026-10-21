"""Provider endpoint URL normalization.

External provider forms commonly receive a base URL with a trailing ``/v1``
because that is how many provider docs spell the API root. Lens' clients add
``v1/...`` themselves, so normalize the base once instead of producing
``/v1/v1/models`` or ``/v1/v1/chat/completions``.
"""

from __future__ import annotations


def normalize_provider_base_url(base_url: str) -> str:
    """Return a trailing-slash base URL with at most one implicit API version.

    ``https://api.example.com``, ``https://api.example.com/`` and
    ``https://api.example.com/v1`` all normalize to
    ``https://api.example.com/``. A non-version path such as
    ``https://api.example.com/openai/v1.0`` is preserved.
    """
    base = base_url.strip()
    if not base:
        return base
    if not base.endswith("/"):
        base = f"{base}/"
    without_slash = base.rstrip("/")
    if without_slash.endswith("/v1"):
        return f"{without_slash[:-3]}/"
    return base

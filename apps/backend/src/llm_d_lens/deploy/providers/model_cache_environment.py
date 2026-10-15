"""Offline Hugging Face environment for serving models downloaded by Model Cache."""

from __future__ import annotations

MODEL_CACHE_MOUNT_PATH = "/model-cache"

_HF_OFFLINE_ENVIRONMENT = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def model_cache_environment(
    model_source: str, *, mount_path: str = MODEL_CACHE_MOUNT_PATH, cache_mounted: bool = False, offline: bool = True
) -> dict[str, str]:
    """Never allow serving Pods to download models, including legacy online inputs.

    ``offline`` and ``cache_mounted`` remain accepted for existing callers but
    cannot disable offline resolution. Model Cache owns all model downloads.
    """
    del offline, cache_mounted
    if model_source == "auto-cache":
        return {"HF_HOME": mount_path, **_HF_OFFLINE_ENVIRONMENT}
    return dict(_HF_OFFLINE_ENVIRONMENT)

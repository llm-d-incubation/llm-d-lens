"""Local in-process Python API for trusted Lens automation."""

from llm_d_lens.auth.context import LensContext
from llm_d_lens.sdk.local import LensLocal

__all__ = ["LensContext", "LensLocal"]

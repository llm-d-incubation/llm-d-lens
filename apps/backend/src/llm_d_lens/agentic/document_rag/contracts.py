"""Document records reuse the persisted immutable shape, not SQL ownership."""

from typing import Literal

from llm_d_lens.agentic.public_benchmark_record.contracts import (
    KIND_FIELDS as KIND_FIELDS,
)
from llm_d_lens.agentic.public_benchmark_record.contracts import (
    Coverage as Coverage,
)
from llm_d_lens.agentic.public_benchmark_record.contracts import (
    ItemKind as ItemKind,
)
from llm_d_lens.agentic.public_benchmark_record.contracts import (
    PublicBenchmarkRecord,
)
from llm_d_lens.agentic.public_benchmark_record.contracts import (
    model_identities_match as model_identities_match,
)
from llm_d_lens.agentic.public_benchmark_record.contracts import (
    model_identity_aliases as model_identity_aliases,
)


class DocumentRecord(PublicBenchmarkRecord):
    kind: Literal["source", "passage"]

"""Stable publication envelopes shared with immutable public records."""

from llm_d_lens.agentic.document_rag.contracts import DocumentRecord
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    InputFormat as InputFormat,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    InventoryFile as InventoryFile,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    ManifestFile as ManifestFile,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    PinnedInventory as PinnedInventory,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    PublicationConflictError as PublicationConflictError,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    PublicationError as PublicationError,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    PublicationEvent as PublicationEvent,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    PublicationModel,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    PublicationResult as PublicationResult,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    RegistryState as RegistryState,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    ReleaseManifest as ReleaseManifest,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    RestoreReceipt as RestoreReceipt,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    SourceApproval as SourceApproval,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    content_digest as content_digest,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    item_id as item_id,
)
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import (
    registry_id as registry_id,
)


class ParsedFile(PublicationModel):
    path: str
    items: tuple[DocumentRecord, ...]
    errors: tuple[str, ...] = ()


class SourceHead(PublicationModel):
    registry: DocumentRecord
    state: RegistryState
    manifest: DocumentRecord | None

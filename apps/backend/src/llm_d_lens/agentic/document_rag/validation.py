"""Document publication validation, independent of SQL DAOs and Prism policy."""

from collections.abc import Callable

from llm_d_lens.agentic.document_rag.contracts import DocumentRecord
from llm_d_lens.agentic.document_rag.publication_contracts import (
    PublicationError,
    ReleaseManifest,
    SourceApproval,
    content_digest,
    item_id,
    registry_id,
)
from llm_d_lens.agentic.document_rag.sources import validate_revision


def validate_manifest(item: DocumentRecord, origin_id: str, approval: SourceApproval) -> ReleaseManifest:
    if (
        item.kind != "source"
        or item.source_role != "manifest"
        or item.origin_id != origin_id
        or item.parent_id != registry_id(origin_id)
    ):
        raise PublicationError("invalid document source manifest")
    metadata = ReleaseManifest.model_validate(item.source_details)
    validate_revision(origin_id, metadata.inventory.revision)
    digest = content_digest(metadata.model_dump(mode="json"))
    if (
        metadata.inventory.origin_id != origin_id
        or metadata.inventory.revision != item.source_revision
        or metadata.parser_version != approval.parser_version
        or item.content_sha256 != digest
        or item.logical_key != f"@manifest:{digest}"
        or item.item_id != item_id(origin_id, item.logical_key, item.source_revision)
        or tuple(item.manifest_item_ids or ()) != metadata.member_ids
    ):
        raise PublicationError("manifest identity, hash, parser, or membership mismatch")
    return metadata


def validate_targets(item: DocumentRecord, lookup: Callable[[str], DocumentRecord | None]) -> None:
    if item.parent_id is not None:
        parent = lookup(item.parent_id)
        role = "registry" if item.kind == "source" else "file"
        if (
            parent is None
            or parent.kind != "source"
            or parent.source_role != role
            or parent.origin_id != item.origin_id
            or (role == "file" and parent.source_revision != item.source_revision)
        ):
            raise PublicationError(f"invalid document {role} link: {item.parent_id}")
    if item.published_manifest_id is not None:
        manifest = lookup(item.published_manifest_id)
        if manifest is None or manifest.source_role != "manifest" or manifest.origin_id != item.origin_id:
            raise PublicationError("invalid published document manifest")

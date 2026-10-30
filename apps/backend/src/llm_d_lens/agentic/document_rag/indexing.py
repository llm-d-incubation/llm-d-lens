"""Build complete lexical projections from fixed, published public manifests."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import PurePosixPath
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from llm_d_lens.agentic.document_rag.contracts import DocumentRecord
from llm_d_lens.agentic.document_rag.document_lifecycle import DocumentLifecycle
from llm_d_lens.agentic.document_rag.document_repository import DocumentRepository
from llm_d_lens.agentic.document_rag.embedding import LocalE5Encoder, passage_input
from llm_d_lens.agentic.document_rag.opensearch_evidence_search import (
    OpenSearchEvidenceProjectionWriter,
    OpenSearchEvidenceSearchRepository,
    technical_identifiers,
)
from llm_d_lens.agentic.document_rag.query_contracts import QueryFilters
from llm_d_lens.agentic.document_rag.search_contracts import (
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
    PublicSearchScope,
)
from llm_d_lens.agentic.document_rag.service import _verify_passage_text
from llm_d_lens.agentic.document_rag.sources import SourceArchive
from llm_d_lens.core.exceptions import ConflictError


def passage_document(item: DocumentRecord, scope: PublicSearchScope) -> PublicPassageDocument:
    if item.kind != "passage" or item.origin_id != scope.origin_id or item.access_classification != "verified_public":
        raise ValueError("only scoped public passages may enter the public index")
    headings = tuple(item.heading_path or ())
    text = item.passage_text
    if not text:
        raise ValueError("public passages must contain text")
    title = headings[-1] if headings else PurePosixPath(item.source_path).name
    return PublicPassageDocument(
        chunk_id=item.item_id,
        evidence_id=item.item_id,
        origin_id=item.origin_id,
        manifest_id=scope.manifest_id,
        publication_generation=scope.generation,
        source_revision=item.source_revision,
        content_sha256=item.content_sha256,
        source_path=item.source_path,
        title=title,
        heading_path=headings,
        text=text,
        identifiers=technical_identifiers(title + "\n" + text),
    )


def passage_projection_matches(
    document: PublicPassageDocument,
    expected: PublicPassageDocument,
    parent: PublicPassageDocument,
    *,
    exclude_embedding: bool = False,
) -> bool:
    """Accept the exact legacy identifier projection, never unverified source changes."""
    legacy = technical_identifiers(parent.title + "\n" + parent.text, expand_aliases=False)
    if document.identifiers == legacy:
        expected = expected.model_copy(update={"identifiers": legacy})
    excluded = {"embedding"} if exclude_embedding else set()
    return document.model_dump(exclude=excluded) == expected.model_dump(exclude=excluded)


class PublicRagIndexBuilder(AbstractContextManager["PublicRagIndexBuilder"]):
    def __init__(
        self,
        repository: OpenSearchEvidenceSearchRepository,
        *,
        dao: DocumentRepository | None = None,
        archive: SourceArchive | None = None,
        lifecycle: DocumentLifecycle | None = None,
    ) -> None:
        self.archive = archive if archive is not None else SourceArchive()
        self.writer = OpenSearchEvidenceProjectionWriter(repository)
        self._owns_dao = dao is None
        self.dao = dao if dao is not None else DocumentRepository.from_environment()
        self.lifecycle = lifecycle if lifecycle is not None else DocumentLifecycle(self.dao)

    def close(self) -> None:
        if self._owns_dao:
            self.dao.close()
            self._owns_dao = False

    def __exit__(self, *_: object) -> None:
        self.close()

    async def build(self, origins: tuple[str, ...], *, encoder: LocalE5Encoder | None = None) -> PublicSearchBuild:
        origins = QueryFilters(origins=origins).origins
        await self.lifecycle.recover(self.writer, vector=encoder is not None)
        scopes = await run_in_threadpool(self.dao.public_search_scopes, origins)
        if {scope.origin_id for scope in scopes} != set(origins):
            raise EvidenceSearchError("Every requested build source must be enabled and published")
        documents = []
        cache: dict[str, bytes] = {}
        for scope in scopes:
            items = await run_in_threadpool(self.dao.list_published, scope.origin_id, kind="passage")
            for item in items:
                if item.content_sha256 not in cache:
                    cache[item.content_sha256] = await run_in_threadpool(self.archive.read, item.content_sha256)
                _verify_passage_text(item, cache[item.content_sha256])
                parent = passage_document(item, scope)
                if encoder is None:
                    documents.append(parent)
                else:
                    documents.extend(await run_in_threadpool(encoder.chunks, parent))
        if not documents or len({document.chunk_id for document in documents}) != len(documents):
            raise EvidenceSearchError("Public index needs nonempty, unique evidence")
        build = PublicSearchBuild(
            build_id=uuid4().hex,
            scopes=scopes,
            document_count=len(documents),
            retrieval_mode="opensearch_vector" if encoder is not None else "opensearch_lexical",
            embedding_contract=encoder.contract if encoder is not None else None,
        )
        index = "public-rag-v1-" + build.build_id
        await run_in_threadpool(self.lifecycle.create, build, index)
        try:
            if await self.writer.create(build) != index:
                raise EvidenceSearchError("Public search writer returned an unexpected physical index")
            for offset in range(0, len(documents), 200):
                batch = documents[offset : offset + 200]
                if encoder is not None:
                    vectors = await run_in_threadpool(encoder.encode, tuple(passage_input(doc) for doc in batch))
                    batch = [
                        document.model_copy(update={"embedding": vector})
                        for document, vector in zip(batch, vectors, strict=True)
                    ]
                await self.writer.write(index, batch)
            await self.writer.verify(index, build.document_count)
            if await self.writer.index_build(index) != build:
                raise EvidenceSearchError("Public search index metadata does not match its build")
            await run_in_threadpool(self.lifecycle.ready, build.build_id, build.document_count)
            if await run_in_threadpool(self.dao.public_search_scopes, origins) != scopes:
                raise ConflictError("Public sources changed while building; the new index was not activated")
            await self.lifecycle.activate(build.build_id, self.writer)
        except Exception:
            await run_in_threadpool(self.lifecycle.fail, build.build_id)
            raise
        return build

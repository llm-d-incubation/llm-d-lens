"""Pinned, explicitly approved guide and benchmark documentation ingestion."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import JsonValue

from llm_d_lens.agentic.document_rag.contracts import DocumentRecord, ItemKind
from llm_d_lens.agentic.document_rag.publication import RagPublicationService
from llm_d_lens.agentic.document_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    PublicationError,
    PublicationResult,
    SourceApproval,
    item_id,
)
from llm_d_lens.agentic.document_rag.sources import (
    BENCHMARK_ORIGIN,
    LLM_D_ORIGIN,
    WEB_SOURCE_PATHS,
    SourceArchive,
    collect_git,
    collect_snapshot,
    validate_approval,
    web_source_url,
)

PARSER_VERSION = "document-passages-v2"
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^\s)\n]+)(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_REFERENCE_IMAGE = re.compile(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]")
_REFERENCE = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?")


@dataclass(frozen=True)
class MarkdownSection:
    heading_path: tuple[str, ...]
    start: int
    end: int
    start_line: int
    end_line: int


def markdown_sections(text: str) -> tuple[MarkdownSection, ...]:
    """Offsets address the decoded pinned text, not another rendering or revision."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, int, tuple[str, ...]]] = [(0, 1, ())]
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    offset = 0
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        marker = _FENCE.match(stripped)
        if fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", stripped):
                fence = None
        elif marker:
            fence = marker[1]
        else:
            heading = _HEADING.match(stripped)
            if heading:
                level = len(heading[1])
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, heading[2]))
                entry = (offset, index + 1, tuple(value for _, value in headings))
                if starts[-1][0] == offset:
                    starts[-1] = entry
                else:
                    starts.append(entry)
        offset += len(line)
    if fence is not None:
        raise PublicationError("unterminated Markdown fenced block")
    return tuple(
        MarkdownSection(
            heading_path=heading_path,
            start=start,
            end=starts[index + 1][0] if index + 1 < len(starts) else len(text),
            start_line=line,
            end_line=starts[index + 1][1] - 1 if index + 1 < len(starts) else len(lines),
        )
        for index, (start, line, heading_path) in enumerate(starts)
        if text[start : starts[index + 1][0] if index + 1 < len(starts) else len(text)].strip()
    )


def _base(file: InventoryFile, inventory: PinnedInventory, key: str) -> dict[str, JsonValue]:
    return {
        "item_id": item_id(inventory.origin_id, key, inventory.revision),
        "logical_key": key,
        "origin_id": inventory.origin_id,
        "source_revision": inventory.revision,
        "content_sha256": file.sha256,
        "access_classification": "verified_public",
        "source_path": file.path,
        "format": file.format,
        **(
            {"source_url": web_source_url(inventory.origin_id, file.path)}
            if inventory.origin_id in WEB_SOURCE_PATHS
            else {}
        ),
    }


def _item(file: InventoryFile, inventory: PinnedInventory, suffix: str, **fields: object) -> DocumentRecord:
    return DocumentRecord.model_validate({**_base(file, inventory, f"{file.path}#{suffix}"), **fields})


class _Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[tuple[str, str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            values = dict(attrs)
            source = values.get("src")
            if source:
                self.images.append((source, values.get("alt") or values.get("title") or "", self.getpos()[0]))


def _local_reference(source: str, target: str) -> str | None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    decoded = unquote(parsed.path)
    if not decoded or decoded.startswith("/") or "\\" in decoded or "\x00" in decoded:
        return None
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
    return None if normalized.startswith("../") or normalized == ".." else normalized


def _image_references(text: str) -> list[tuple[str, str, int]]:
    visible: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line.rstrip("\r\n"))
        if fence is None and marker:
            fence = marker[1]
            visible.append("\n")
        elif fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line.rstrip("\r\n")):
                fence = None
            visible.append("\n")
        else:
            visible.append(line)
    rendered = "".join(visible)
    html = _Images()
    html.feed(rendered)
    results = list(html.images)
    references = {match[1].casefold(): match[2] for line in visible if (match := _REFERENCE.match(line)) is not None}
    for index, line in enumerate(visible, 1):
        results.extend((match[2].strip("<>"), match[1], index) for match in _IMAGE.finditer(line))
        for match in _REFERENCE_IMAGE.finditer(line):
            label = (match[2] or match[1]).casefold()
            if label not in references:
                raise PublicationError("unresolved Markdown image reference")
            results.append((references[label], match[1], index))
    return sorted(set(results), key=lambda entry: (entry[2], entry[0], entry[1]))


def parse_markdown(file: InventoryFile, inventory: PinnedInventory, data: bytes) -> tuple[DocumentRecord, ...]:
    if hashlib.sha256(data).hexdigest() != file.sha256:
        raise PublicationError("Markdown bytes do not match the pinned source hash")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise PublicationError("Markdown is not UTF-8") from error
    sections = markdown_sections(text)
    references = _image_references(text)
    eligible = {entry.path for entry in inventory.files}
    result = []
    for section in sections:
        excerpt = text[section.start : section.end]
        images: list[JsonValue] = [
            {
                "target": target,
                "resolved_path": _local_reference(file.path, target),
                "caption": caption,
                "line": line,
                "in_inventory": _local_reference(file.path, target) in eligible,
            }
            for target, caption, line in references
            if section.start_line <= line <= section.end_line
        ]
        result.append(
            _item(
                file,
                inventory,
                f"section:{section.start}",
                kind="passage",
                parent_id=item_id(inventory.origin_id, f"file:{file.path}", inventory.revision),
                passage_text=excerpt,
                heading_path=list(section.heading_path),
                source_pointer={
                    "start_char": section.start,
                    "end_char": section.end,
                    "start_line": section.start_line,
                    "end_line": section.end_line,
                    "offset_encoding": "unicode_codepoints",
                    "evidence_role": (
                        "published_article_context"
                        if inventory.origin_id in WEB_SOURCE_PATHS
                        else "documentation"
                        if inventory.origin_id == BENCHMARK_ORIGIN
                        else "guide_context"
                    ),
                    **(
                        {"source_url": web_source_url(inventory.origin_id, file.path)}
                        if inventory.origin_id in WEB_SOURCE_PATHS
                        else {}
                    ),
                    "contains_code_examples": bool(re.search(r"(?m)^ {0,3}(?:`{3,}|~{3,})", excerpt)),
                    "image_references": images,
                },
            )
        )
    return tuple(result)


def parse_repository(inventory: PinnedInventory, archive: SourceArchive) -> tuple[ParsedFile, ...]:
    if inventory.origin_id not in (LLM_D_ORIGIN, BENCHMARK_ORIGIN):
        raise PublicationError("repository parser only supports the two approved repository origins")
    parsed = []
    for file in inventory.files:
        data = archive.read(file.sha256)
        if file.format == "markdown":
            items = parse_markdown(file, inventory, data)
        elif file.format == "json":
            import json

            try:
                json.loads(data)
            except (ValueError, UnicodeError) as error:
                raise PublicationError("pinned repository JSON is invalid") from error
            items = ()
        elif file.format == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
            items = ()
        else:
            raise PublicationError("repository file does not match an approved document format")
        parsed.append(ParsedFile(path=file.path, items=items))
    return tuple(parsed)


def repository_counts(parsed: tuple[ParsedFile, ...]) -> dict[ItemKind, int]:
    counts: Counter[ItemKind] = Counter(item.kind for file in parsed for item in file.items)
    if parsed:
        counts["source"] = len(parsed)
    return dict(counts)


class ReviewedWebSnapshotImporter(AbstractContextManager["ReviewedWebSnapshotImporter"]):
    def __init__(self, publication: RagPublicationService | None = None) -> None:
        self._owns_publication = publication is None
        self.publication = publication if publication is not None else RagPublicationService()

    def close(self) -> None:
        if self._owns_publication:
            self.publication.close()
            self._owns_publication = False

    def __exit__(self, *_: object) -> None:
        self.close()

    def stage_snapshot(
        self,
        root: Path,
        inventory: PinnedInventory,
        approval: SourceApproval,
        *,
        expected_counts: dict[ItemKind, int],
    ) -> DocumentRecord:
        """Stage operator-reviewed Markdown only, without fetching URLs or extracting measurements."""
        if inventory.origin_id not in WEB_SOURCE_PATHS or approval.parser_version != PARSER_VERSION:
            raise PublicationError("web snapshot approval needs a supported origin and parser version")
        collected = collect_snapshot(
            root,
            inventory.revision,
            {file.path: file.sha256 for file in inventory.files},
            approval,
            self.publication.archive,
            origin_id=inventory.origin_id,
        )
        if collected != inventory:
            raise PublicationError("web snapshot differs from the reviewed Markdown inventory")
        parsed = tuple(
            ParsedFile(
                path=file.path,
                items=parse_markdown(file, collected, self.publication.archive.read(file.sha256)),
            )
            for file in collected.files
        )
        if repository_counts(parsed) != {kind: count for kind, count in expected_counts.items() if count}:
            raise PublicationError("web snapshot extraction differs from reviewed per-kind counts")
        self.publication.register_source(inventory.origin_id, approval)
        return self.publication.stage(collected, parsed, expected_counts=expected_counts)


class RepositoryCorpusImporter(AbstractContextManager["RepositoryCorpusImporter"]):
    def __init__(self, publication: RagPublicationService | None = None) -> None:
        self._owns_publication = publication is None
        self.publication = publication if publication is not None else RagPublicationService()

    def close(self) -> None:
        if self._owns_publication:
            self.publication.close()
            self._owns_publication = False

    def __exit__(self, *_: object) -> None:
        self.close()

    def stage_repository(
        self,
        repo: Path | None,
        origin_id: str,
        revision: str,
        approval: SourceApproval,
        *,
        expected_counts: dict[ItemKind, int],
        expected_inventory_digest: str | None = None,
    ) -> DocumentRecord:
        """Stage reviewed document counts and bytes without publishing.

        A None repo fetches the approved GitHub source into the managed cache.
        No numeric experiment or figure records are extracted.
        """
        if origin_id not in (LLM_D_ORIGIN, BENCHMARK_ORIGIN) or approval.parser_version != PARSER_VERSION:
            raise PublicationError("repository approval needs the supported origin and parser version")
        validate_approval(origin_id, approval)
        inventory = collect_git(origin_id, repo, revision, approval, self.publication.archive)
        if expected_inventory_digest is not None and inventory.digest != expected_inventory_digest:
            raise PublicationError("repository inventory differs from the reviewed pinned inventory")
        parsed = parse_repository(
            inventory,
            self.publication.archive,
        )
        if repository_counts(parsed) != {kind: count for kind, count in expected_counts.items() if count}:
            raise PublicationError("repository extraction differs from reviewed per-kind counts")
        self.publication.register_source(origin_id, approval)
        return self.publication.stage(inventory, parsed, expected_counts=expected_counts)

    def import_repository(
        self,
        repo: Path | None,
        origin_id: str,
        revision: str,
        approval: SourceApproval,
        *,
        expected_counts: dict[ItemKind, int],
        expected_inventory_digest: str | None = None,
    ) -> PublicationResult:
        """Stage and activate a complete pinned, passage-only repository."""
        manifest = self.stage_repository(
            repo,
            origin_id,
            revision,
            approval,
            expected_counts=expected_counts,
            expected_inventory_digest=expected_inventory_digest,
        )
        return self.publication.activate(origin_id, manifest.item_id)
